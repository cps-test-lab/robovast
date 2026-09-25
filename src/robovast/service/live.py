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

"""A run's tables as it records, handed to subscribers in the data-plane process.

:class:`LiveCampaigns` keeps one :class:`~robovast_decode.live.Watcher` per campaign
somebody is reading live, each driven by a daemon thread that runs the watcher over an
inotify watch of the campaign directory
(:class:`~robovast.execution.data.file_agent.Inotify`). It runs in the data-plane process
on purpose: that is the process the pods' deliveries land in, locally ``vast serve``'s
one process and on the cluster the data container, so the appends this process itself
writes wake the watcher through inotify like any other write, and no second process has
to be told.

A :class:`Subscription` is a bounded queue between the watcher's thread and whoever reads
it. A reader that falls :data:`QUEUE_MAX` batches behind is dropped, with the reason on
its next read, rather than buffered without bound: the batches keep coming at the
recorder's pace whether or not the client keeps up, and memory is what a slow client
would otherwise spend for everyone.

A watcher lives while it has subscribers, and past that for :data:`IDLE_S` seconds so the
next reader of the same campaign does not pay to start it again. Once the campaign is
finished -- its terminal record is written, or its directory is gone -- and the grace has
passed, the watcher is stopped and forgotten.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Set, Tuple

from robovast_decode.live import Batch, Watcher
from robovast_decode.runs import campaign_finished, is_live

logger = logging.getLogger(__name__)

#: Batches a subscriber may leave unread before it is dropped.
QUEUE_MAX = 256

#: How long a watcher whose campaign is finished is kept after its last subscriber left.
IDLE_S = 60.0

#: How often idle watchers are looked for.
REAP_S = 5.0


class _Eof:
    """The end of a subscription: the run finished and its sessions were finalised."""

    def __repr__(self) -> str:
        return "EOF"


#: What :meth:`Subscription.next` returns once nothing more will come.
EOF = _Eof()

_RUN_KEY = re.compile(r"^([^/]+)/(\d+)$")
_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Dropped(RuntimeError):
    """The service ended the subscription, for the reason the message states."""


def parse_run(run: str) -> Tuple[str, int]:
    """``(config_name, run_id)`` of a ``<config>/<run_id>`` key; ``ValueError`` otherwise."""
    match = _RUN_KEY.match(run or "")
    if match is None:
        raise ValueError(f"a run is named <config>/<run_id>, not {run!r}")
    return match.group(1), int(match.group(2))


def check_tables(tables: Iterable[str]) -> List[str]:
    """*tables* as a list of table names; ``ValueError`` when empty or not names."""
    names = [t for t in tables if t]
    if not names:
        raise ValueError("tables names at least one table")
    bad = [t for t in names if not _TABLE.match(t)]
    if bad:
        raise ValueError(f"not table names: {', '.join(repr(t) for t in bad)}")
    return names


class Subscription:
    """One reader's queue of a run's batches, ended by :data:`EOF` or a :class:`Dropped`.

    Filled from the watcher's thread (:meth:`deliver`, :meth:`finish`); read from any
    other with :meth:`next`. :meth:`close` stops the delivery; it is called for the reader
    when it is dropped, and is safe to call again.
    """

    def __init__(self, run: str, tables: List[str], maxsize: int):
        self.run = run
        self.tables = tables
        self._queue: "queue.Queue" = queue.Queue(maxsize=maxsize)
        self._dropped: Optional[str] = None
        self._eof = False
        self._closed = False
        self._release: Optional[Callable[[], None]] = None

    @property
    def dropped(self) -> Optional[str]:
        """Why the service ended this subscription, or ``None`` while it stands."""
        return self._dropped

    # -- the watcher's side ----------------------------------------------------------------

    def deliver(self, batch: Batch) -> None:
        try:
            self._queue.put_nowait(batch)
        except queue.Full:
            self.drop(f"the subscriber to {self.run} fell {self._queue.maxsize} batches "
                      "behind and was dropped: a live stream is not buffered without bound")

    def finish(self) -> None:
        try:
            self._queue.put_nowait(EOF)
        except queue.Full:
            self.drop(f"the subscriber to {self.run} fell {self._queue.maxsize} batches "
                      "behind when the run finished and was dropped")

    def drop(self, reason: str) -> None:
        """End the subscription: *reason* is raised by the reader's next :meth:`next`."""
        if self._dropped is None:
            self._dropped = reason
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self.close()

    # -- the reader's side -----------------------------------------------------------------

    def next(self, timeout: Optional[float] = None):
        """The next :class:`~robovast_decode.live.Batch`, :data:`EOF` at the end, or
        ``None`` when *timeout* seconds passed with nothing; :class:`Dropped` once the
        service ended the subscription."""
        if self._dropped is not None:
            raise Dropped(self._dropped)
        if self._eof:
            return EOF
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            if self._dropped is not None:
                raise Dropped(self._dropped) from None
            return None
        if item is EOF:
            self._eof = True
        return item

    def close(self) -> None:
        """Stop receiving. Idempotent."""
        if self._closed:
            return
        self._closed = True
        release, self._release = self._release, None
        if release is not None:
            release()


@dataclass
class _Campaign:
    campaign_id: str
    campaign_dir: str
    watcher: Watcher
    inotify: object
    thread: Optional[threading.Thread] = None
    subscriptions: Set[Subscription] = field(default_factory=set)
    #: When the last subscriber left; ``None`` while one is there.
    idle_since: Optional[float] = None


class LiveCampaigns:
    """The watchers of one results root, one per campaign being read live.

    :meth:`for_root` gives the one instance per root in this process, so the routes reach the
    same watchers whichever object they were called on.
    """

    _shared: dict = {}
    _shared_lock = threading.Lock()

    @classmethod
    def for_root(cls, results_root) -> "LiveCampaigns":
        """The instance for *results_root* in this process, made on first use."""
        root = os.path.abspath(str(results_root))
        with cls._shared_lock:
            live = cls._shared.get(root)
            if live is None:
                live = cls._shared[root] = cls(root)
            return live

    def __init__(self, results_root):
        self.results_root = os.path.abspath(str(results_root))
        self._lock = threading.RLock()
        self._campaigns: dict = {}
        self._reaper: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    # -- what is asked for -------------------------------------------------------------

    def campaign_dir(self, campaign_id: str) -> str:
        """The campaign's directory under the root; ``KeyError`` when it is not there."""
        if not campaign_id or "/" in campaign_id or campaign_id.startswith("."):
            raise KeyError(f"no campaign {campaign_id!r} on this service")
        path = os.path.join(self.results_root, campaign_id)
        if not os.path.isdir(path):
            raise KeyError(f"no campaign {campaign_id!r} on this service")
        return path

    def subscribe(self, campaign_id: str, run: str, tables: Iterable[str]) -> Subscription:
        """A subscription to *tables* of *run* (``<config>/<run_id>``) of the campaign.

        ``KeyError`` for a campaign or run that is not here, ``ValueError`` for a run key
        or table list that is not one. A run that is not live
        (:func:`~robovast_decode.runs.is_live`) gets a subscription at its end already:
        its rows are all there for a query, and no watcher is started for it.
        """
        campaign_dir = self.campaign_dir(campaign_id)
        config_name, run_id = parse_run(run)
        names = check_tables(tables)
        key = f"{config_name}/{run_id}"
        if not os.path.isdir(os.path.join(campaign_dir, config_name, str(run_id))):
            raise KeyError(f"no run {key!r} in campaign {campaign_id!r}")
        subscription = Subscription(key, names, QUEUE_MAX)
        if not is_live(campaign_dir, config_name, run_id):
            subscription.finish()
            return subscription
        with self._lock:
            entry = self._campaigns.get(campaign_id)
            if entry is None:
                entry = self._start(campaign_id, campaign_dir)
            entry.subscriptions.add(subscription)
            entry.idle_since = None
        unsubscribe: Optional[Callable[[], None]] = None

        def release():
            if unsubscribe is not None:
                unsubscribe()
            with self._lock:
                entry.subscriptions.discard(subscription)
                if not entry.subscriptions:
                    entry.idle_since = time.monotonic()

        subscription._release = release  # pylint: disable=protected-access
        # The watcher may push what is already decoded, and drop the subscriber, before
        # this returns; the unsubscribe is then done here.
        unsubscribe = entry.watcher.subscribe(key, names, subscription.deliver,
                                              subscription.finish)
        if subscription.dropped is not None:
            unsubscribe()
        return subscription

    def frame_index(self, campaign_id: str, run: str, topic: str):
        """The watcher's :class:`~robovast_decode.frames.FrameTap` on *topic* of the live
        run *run* (``<config>/<run_id>``), demanded if it was not; ``None`` while the
        run's recording has not started. The campaign's watcher is started for it as for a
        subscription, and kept as long as one is. ``KeyError`` for a campaign or run that
        is not here, ``ValueError`` for a run key that is not one.
        """
        campaign_dir = self.campaign_dir(campaign_id)
        config_name, run_id = parse_run(run)
        key = f"{config_name}/{run_id}"
        if not os.path.isdir(os.path.join(campaign_dir, config_name, str(run_id))):
            raise KeyError(f"no run {key!r} in campaign {campaign_id!r}")
        with self._lock:
            entry = self._campaigns.get(campaign_id)
            if entry is None:
                entry = self._start(campaign_id, campaign_dir)
            if not entry.subscriptions:
                entry.idle_since = time.monotonic()
        return entry.watcher.frame_index(key, topic)

    def active(self) -> List[str]:
        """The campaigns with a watcher right now."""
        with self._lock:
            return sorted(self._campaigns)

    # -- the watchers ------------------------------------------------------------------

    def _start(self, campaign_id: str, campaign_dir: str) -> _Campaign:
        from robovast.execution.data.file_agent import Inotify  # pylint: disable=import-outside-toplevel
        entry = _Campaign(campaign_id, campaign_dir, Watcher(campaign_dir), Inotify())
        entry.thread = threading.Thread(target=self._run, args=(entry,), daemon=True,
                                        name=f"live:{campaign_id}")
        self._campaigns[campaign_id] = entry
        entry.thread.start()
        if self._reaper is None or not self._reaper.is_alive():
            self._stopping.clear()
            self._reaper = threading.Thread(target=self._reap_forever, daemon=True,
                                            name="live:reaper")
            self._reaper.start()
        logger.info("following %s live", campaign_id)
        return entry

    def _run(self, entry: _Campaign) -> None:
        try:
            entry.watcher.run_forever(entry.inotify)
        except Exception as exc:  # noqa: BLE001 - reported to every subscriber, then re-raised
            logger.exception("the live watcher for %s failed", entry.campaign_id)
            reason = (f"the live watcher for {entry.campaign_id} failed: "
                      f"{type(exc).__name__}: {exc}")
            with self._lock:
                if self._campaigns.get(entry.campaign_id) is entry:
                    del self._campaigns[entry.campaign_id]
                subscriptions = list(entry.subscriptions)
            for subscription in subscriptions:
                subscription.drop(reason)
            raise
        finally:
            entry.inotify.close()

    def _stop_entry(self, entry: _Campaign) -> None:
        entry.watcher.stop()
        if entry.thread is not None and entry.thread is not threading.current_thread():
            entry.thread.join(5.0)
        logger.info("stopped following %s", entry.campaign_id)

    def reap(self, now: Optional[float] = None) -> List[str]:
        """Stop the watchers of finished campaigns idle for :data:`IDLE_S`; their ids."""
        now = time.monotonic() if now is None else now
        due = []
        with self._lock:
            for campaign_id, entry in list(self._campaigns.items()):
                if entry.subscriptions or entry.idle_since is None:
                    continue
                if now - entry.idle_since < IDLE_S:
                    continue
                if os.path.isdir(entry.campaign_dir) and not campaign_finished(
                        entry.campaign_dir):
                    continue
                del self._campaigns[campaign_id]
                due.append(entry)
        for entry in due:
            self._stop_entry(entry)
        return [entry.campaign_id for entry in due]

    def _reap_forever(self) -> None:
        while not self._stopping.wait(REAP_S):
            try:
                self.reap()
            except Exception:  # noqa: BLE001 - one failed sweep must not end the reaper
                logger.exception("reaping idle live watchers failed")

    def stop(self) -> None:
        """Stop every watcher and the reaper. Subscribers are told they were dropped."""
        self._stopping.set()
        with self._lock:
            entries = list(self._campaigns.values())
            self._campaigns.clear()
        for entry in entries:
            for subscription in list(entry.subscriptions):
                subscription.drop(f"the service stopped following {entry.campaign_id}")
            self._stop_entry(entry)
        reaper, self._reaper = self._reaper, None
        if reaper is not None and reaper is not threading.current_thread():
            reaper.join(5.0)


__all__ = ["EOF", "Dropped", "IDLE_S", "LiveCampaigns", "QUEUE_MAX", "REAP_S", "Subscription",
           "check_tables", "parse_run"]
