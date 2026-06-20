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

The in-pod campaign controller pushes a notification for each lifecycle event
(start, batch finished, finished, uploaded, failure) plus an hourly heartbeat, so
detached cluster runs are no longer silent. The topic is configured per-user via
``ROBOVAST_NTFY_TOPIC`` in the project ``.env`` (the host launcher injects it into
the controller pod), so different users get their own topics.

Every send is **best-effort**: it swallows all exceptions and uses a short timeout,
so a misconfigured/unreachable ntfy server can never break or delay a campaign. When
``ROBOVAST_NTFY_TOPIC`` is unset the :class:`Notifier` is a no-op.

Each controller pod builds its own :class:`Notifier` bound to its ``campaign_id`` —
concurrent campaigns report independently (no shared state), and every message
carries the ``campaign_id`` in its title so campaigns sharing one topic stay
distinguishable.
"""

import logging
import os
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_SERVER = "https://ntfy.sh"
_DEFAULT_HEARTBEAT_S = 3600

# Status snapshot for the heartbeat: (batch, completed, total, batches_done).
StatusTuple = tuple[int, int, int, int]


class Notifier:
    """Sends ntfy notifications for one campaign. Disabled instances are no-ops."""

    def __init__(self, campaign_id: str, *, topic: str = "",
                 server: str = _DEFAULT_SERVER, token: str = ""):
        self.campaign_id = campaign_id
        self.topic = (topic or "").strip()
        self.server = (server or _DEFAULT_SERVER).strip().rstrip("/")
        self.token = (token or "").strip()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()

    @classmethod
    def from_env(cls, campaign_id: str) -> "Notifier":
        """Build a Notifier bound to *campaign_id* from the ``ROBOVAST_NTFY_*`` env.

        Returns a disabled (no-op) instance when ``ROBOVAST_NTFY_TOPIC`` is unset.
        """
        return cls(
            campaign_id,
            topic=os.environ.get("ROBOVAST_NTFY_TOPIC", ""),
            server=os.environ.get("ROBOVAST_NTFY_SERVER", "") or _DEFAULT_SERVER,
            token=os.environ.get("ROBOVAST_NTFY_TOKEN", ""),
        )

    @property
    def enabled(self) -> bool:
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

    # -- lifecycle events ---------------------------------------------------

    def started(self, mode: str) -> None:
        self._send(f"Campaign started ({mode}).", priority=3, tags="rocket")

    def batch_finished(self, idx: int, n_units: int) -> None:
        self._send(f"Batch {idx} finished — {n_units} unit(s).",
                   priority=2, tags="white_check_mark")

    def finished(self, summary: str) -> None:
        self._send(f"Campaign finished. {summary}", priority=3,
                   tags="checkered_flag")

    def uploaded(self, share_type: str) -> None:
        self._send(f"Campaign uploaded to share ({share_type}).",
                   priority=3, tags="outbox_tray")

    def failed(self, reason: str) -> None:
        self._send(f"Campaign FAILED: {reason}", priority=5,
                   tags="rotating_light")

    # -- hourly heartbeat ---------------------------------------------------

    def start_heartbeat(self, status_fn: Callable[[], Optional[StatusTuple]],
                        interval: float = _DEFAULT_HEARTBEAT_S) -> None:
        """Start a daemon thread that periodically reports run progress.

        *status_fn* returns ``(batch, completed, total, batches_done)`` or ``None``
        when progress is not yet available. No-op when notifications are disabled.
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
