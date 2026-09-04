# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Best-effort `ntfy.sh <https://ntfy.sh>`_ push notifications for a campaign.

The campaign driver pushes a notification for each lifecycle event (start, batch
finished, finished, uploaded, failure) plus an hourly heartbeat, so a detached cluster
run is not silent. The topic is configured per-user via ``ROBOVAST_NTFY_TOPIC``
in the project ``.env``, so different users get their own topics. The driver runs
in-process inside ``robovast-service``; for an in-cluster service the ntfy env is read
from the host ``.env`` at setup and injected into the service pod via a Secret +
``envFrom`` (see ``service_deploy.ENV_SECRET_SOURCES``), so :meth:`Notifier.from_env`
picks it up from ``os.environ`` — a local ``vast serve`` reads the same env directly.

Every send is **best-effort**: it swallows all exceptions and uses a short timeout,
so a misconfigured/unreachable ntfy server can never break or delay a campaign. When
``ROBOVAST_NTFY_TOPIC`` is unset the :class:`Notifier` is a no-op.

Each campaign builds its own :class:`Notifier` bound to its ``campaign_id`` —
concurrent campaigns report independently (no shared state), and every message
carries the ``campaign_id`` in its title so campaigns sharing one topic stay
distinguishable.

**Two sinks, one place.** ntfy leaves the machine and is gone once read; a campaign that
started, failed or was stopped last week is then a fact nobody holds. So every lifecycle
announcement also goes to the service's durable event log, passed in as ``events`` by
whoever builds the notifier and ``None`` where there is no service (a CLI run has nowhere
durable to write, and must not need one to exist). Driving both from
:meth:`Notifier._announce` is what stops an event reaching the phone and missing the record.
"""

import logging
import os
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: Public ntfy instance used when ``ROBOVAST_NTFY_SERVER`` names none. Public so the
#: service configuration report can state the default rather than restate the literal.
DEFAULT_SERVER = "https://ntfy.sh"
_DEFAULT_HEARTBEAT_S = 3600

# Status snapshot for the heartbeat: (batch, completed, total, batches_done).
StatusTuple = tuple[int, int, int, int]


class Notifier:
    """Announces one campaign's lifecycle: an ntfy push, and the durable record beside it.

    "Disabled" means the *push* is unconfigured, which makes only that half a no-op — the
    record is a separate sink, and whether a campaign's life is kept must not depend on
    whether somebody set up a phone topic.
    """

    def __init__(self, campaign_id: str, *, topic: str = "",
                 server: str = DEFAULT_SERVER, token: str = "", events=None):
        self.campaign_id = campaign_id
        #: The durable record the announcements are also written to, or ``None``.
        #: Duck-typed on ``EventLog.append`` rather than imported: the execution layer
        #: runs with no service around it, and must not depend on one to say what it did.
        self.events = events
        self.topic = (topic or "").strip()
        self.server = (server or DEFAULT_SERVER).strip().rstrip("/")
        self.token = (token or "").strip()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()
        # A campaign announces its end once. More than one scope may legitimately try:
        # the builders' finish tail ends the campaign on the lanes it is outermost for,
        # while the service worker ends it unconditionally because a campaign that failed
        # before the builder ran (an image build that could not resolve) would otherwise
        # never end at all. Both are correct; two "Campaign finished" pushes are not.
        self._terminal_sent = False

    @classmethod
    def from_env(cls, campaign_id: str, *, events=None) -> "Notifier":
        """Build a Notifier bound to *campaign_id* from the ``ROBOVAST_NTFY_*`` env.

        Returns a *push*-disabled instance when ``ROBOVAST_NTFY_TOPIC`` is unset. Only the
        push: *events* is not read from the environment because it is not configuration,
        and a campaign still records what it did on a service that pushes nowhere.
        """
        return cls(
            campaign_id,
            topic=os.environ.get("ROBOVAST_NTFY_TOPIC", ""),
            server=os.environ.get("ROBOVAST_NTFY_SERVER", "") or DEFAULT_SERVER,
            token=os.environ.get("ROBOVAST_NTFY_TOKEN", ""),
            events=events,
        )

    @property
    def enabled(self) -> bool:
        """Whether the *push* is configured. Says nothing about the durable record, which
        is a separate sink and is written whether or not anything is pushed anywhere."""
        return bool(self.topic)

    # -- wire ---------------------------------------------------------------

    def _send(self, message: str, *, priority: int, tags: str) -> None:
        """POST one notification to ``{server}/{topic}``. Never raises."""
        if not self.enabled:
            return
        try:
            import requests  # pylint: disable=import-outside-toplevel

            headers = {
                "Title": self.campaign_id,
                "Priority": str(priority),
                "Tags": tags,
            }
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            requests.post(
                f"{self.server}/{self.topic}",
                # Restate the campaign id in the body so it survives clients that
                # hide the title (and groups messages on a shared topic).
                data=f"[{self.campaign_id}] {message}".encode(),
                headers=headers, timeout=5,
            )
        except Exception:  # pylint: disable=broad-except
            logger.debug("ntfy notification failed", exc_info=True)

    def _record(self, kind: str, message: str, severity: str) -> None:
        """Write the same announcement to the durable record. Never raises.

        Best-effort like the push beside it, and for the same reason: this describes the
        work, it is not the work, so a log that cannot be written must not end a campaign.
        """
        if self.events is None:
            return
        try:
            self.events.append(kind, message=message, severity=severity,
                               subject_type="campaign", subject_id=self.campaign_id)
        except Exception:  # pylint: disable=broad-except
            logger.debug("could not record a %s event", kind, exc_info=True)

    def _announce(self, kind: str, message: str, *, severity: str, priority: int,
                  tags: str) -> None:
        """One lifecycle event, to both sinks.

        The record first, then the push: the push reaches out over the network with a
        timeout, and the fact is worth keeping even when the reaching-out is what fails.
        """
        self._record(kind, message, severity)
        self._send(message, priority=priority, tags=tags)

    # -- lifecycle events ---------------------------------------------------

    def started(self, mode: str) -> None:
        self._announce("campaign.started", f"Campaign started ({mode}).",
                       severity="info", priority=3, tags="rocket")

    def batch_finished(self, idx: int, n_units: int) -> None:
        self._announce("campaign.batch_finished",
                       f"Batch {idx} finished — {n_units} unit(s).",
                       severity="info", priority=2, tags="white_check_mark")

    def finished(self, summary: str, *, degraded: bool = False) -> None:
        """The campaign is over. *summary* says what it actually produced.

        ``degraded`` is not cosmetic: a campaign whose trials passed but whose
        postprocessing failed still finishes, with no CSVs and nothing queryable. Sent at
        the same priority and tag as a clean finish, it read as success on the phone —
        the one place nobody goes back to re-read.
        """
        if degraded:
            self._announce_terminal("campaign.finished",
                                    f"Campaign finished WITH PROBLEMS. {summary}",
                                    severity="warning", priority=4, tags="warning")
        else:
            self._announce_terminal("campaign.finished", f"Campaign finished. {summary}",
                                    severity="success", priority=3,
                                    tags="checkered_flag")

    def stopped(self, summary: str) -> None:
        """The campaign was stopped by request. Its own event, because silence here was
        indistinguishable from a campaign still running."""
        self._announce_terminal("campaign.stopped",
                                f"Campaign STOPPED by request. {summary}",
                                severity="warning", priority=4, tags="octagonal_sign")

    def retriggered(self, new_campaign_id: str) -> None:
        """A re-run of this campaign was launched as *new_campaign_id*.

        Sent from the **source** campaign's notifier, so a topic watching a long-running
        campaign learns where its re-run went. Not terminal: the source is unchanged and
        keeps whatever end-of-life message it is going to send, and the new campaign
        announces its own :meth:`started` separately.
        """
        self._announce("campaign.retriggered", f"Retriggered as {new_campaign_id}.",
                       severity="info", priority=3, tags="repeat")

    def uploaded(self, share_type: str) -> None:
        self._announce("campaign.uploaded",
                       f"Campaign uploaded to share ({share_type}).",
                       severity="success", priority=3, tags="outbox_tray")

    def upload_failed(self, reason: str) -> None:
        """An upload-to-share failed. **Not** terminal, and deliberately not :meth:`failed`.

        A share failure leaves the campaign finished — its trials ran and its data is on
        disk; what failed is the copy to somebody else's storage, and it can be
        re-triggered. Sending :meth:`failed` here would both claim the campaign died and
        burn its one end-of-life message on something that is not the end of it.
        """
        self._announce("campaign.upload_failed", f"Upload to share FAILED: {reason}",
                       severity="warning", priority=4, tags="warning")

    def postprocessed(self) -> None:
        """A re-run of postprocessing produced its derived data."""
        self._announce("campaign.postprocessed", "Postprocessing complete.",
                       severity="success", priority=3, tags="bar_chart")

    def postprocessing_failed(self, reason: str) -> None:
        """A re-run of postprocessing failed. Not terminal, for the same reason as
        :meth:`upload_failed`: the campaign's trials are unaffected and on disk, and the
        step can be re-triggered. Within a campaign this case is instead folded into
        :meth:`finished` as ``degraded`` -- there it IS the campaign's ending.
        """
        self._announce("campaign.postprocessing_failed",
                       f"Postprocessing FAILED: {reason}",
                       severity="warning", priority=4, tags="warning")

    def postprocessing_cancelled(self, reason: str) -> None:
        """A re-run of postprocessing was cancelled by a stop.

        Its own event rather than :meth:`postprocessing_failed`, for the same reason a
        stopped campaign is not a failed one: the operator asked for this, and announcing
        it as a failure files a deliberate act under faults and sends whoever reads it
        looking for a fault that is not there. Not terminal — the campaign's trials are
        untouched and the step can be asked for again.
        """
        self._announce("campaign.postprocessing_cancelled",
                       f"Postprocessing CANCELLED: {reason}",
                       severity="warning", priority=3, tags="octagonal_sign")

    def failed(self, reason: str) -> None:
        self._announce_terminal("campaign.failed", f"Campaign FAILED: {reason}",
                                severity="error", priority=5, tags="rotating_light")

    def _announce_terminal(self, kind: str, message: str, *, severity: str, priority: int,
                           tags: str) -> None:
        """Announce the campaign's one end-of-life event; ignore any later one.

        Guarded rather than left to the callers because more than one scope may
        legitimately end a campaign (see :attr:`_terminal_sent`), and only the first of
        them is describing anything new. Making that the notifier's own invariant means
        no future caller can break it by being correct about something else.

        The guard covers both sinks, so the record agrees with the phone about how a
        campaign ended rather than carrying every scope's opinion of it.
        """
        if self._terminal_sent:
            return
        self._terminal_sent = True
        self._announce(kind, message, severity=severity, priority=priority, tags=tags)

    # -- hourly heartbeat ---------------------------------------------------

    def start_heartbeat(self, status_fn: Callable[[], Optional[StatusTuple]],
                        interval: float = _DEFAULT_HEARTBEAT_S) -> None:
        """Start a daemon thread that periodically reports run progress.

        *status_fn* returns ``(batch, completed, total, batches_done)`` or ``None``
        when progress is not yet available. No-op when notifications are disabled.

        Push only, deliberately: a heartbeat says the campaign is still alive *now*, which
        is worth interrupting someone with and worthless once it is over. Recording one an
        hour would fill the durable log with the one thing nobody reads back.
        """
        if not self.enabled or self._heartbeat_thread is not None:
            return

        def _beat() -> None:
            while not self._heartbeat_stop.wait(interval):
                try:
                    status = status_fn()
                except Exception:  # pylint: disable=broad-except
                    status = None
                if status is None:
                    continue
                batch, completed, total, batches_done = status
                self._send(
                    f"Progress: batch {batch} — {completed}/{total} runs "
                    f"({batches_done} batch(es) done).",
                    priority=2, tags="hourglass_flowing_sand")

        self._heartbeat_thread = threading.Thread(
            target=_beat, name="robovast-ntfy-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
