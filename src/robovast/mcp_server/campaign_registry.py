#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
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

"""Crash-safe run-state registry for the ``campaign_control`` MCP plugin.

RoboVAST has no local lock, PID registry, or single-flight mechanism: two
concurrent ``vast exec local run`` invocations collide on the shared Docker
resources they use (a hardcoded ``container_name: robovast`` and a fixed
compose-file path). This registry provides the missing "is a campaign running?"
answer and, for the local backend, refuses a second launch while one is live.

Design:

* One JSON file, ``<results_dir>/_control/registry.json``, mapping
  ``campaign_id`` to an entry dict.
* Every read-modify-write happens under an advisory ``fcntl.flock`` on
  ``<results_dir>/_control/registry.lock`` and is committed via a temp file +
  ``os.rename`` (atomic on the same filesystem), so two concurrent MCP calls
  cannot interleave a launch.
* Local liveness is derived from ``/proc``: a recorded-running entry is live
  only if its pid is alive **and** the process start-time matches **and** the
  cmdline still carries ``--campaign-id <id>``. This defeats PID reuse and
  reconciles a killed sweep to a terminal state so it never blocks forever.

The registry is deliberately free of any Kubernetes / MCP imports so it is
unit-testable on its own. Finished-vs-crashed classification of a dead local
entry is delegated to a ``classify_dead_local`` callback supplied by the caller
(the plugin reads ``campaign.db`` / on-disk aggregates); the default is
``"crashed"``.
"""

import contextlib
import fcntl
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

CONTROL_DIRNAME = "_control"
REGISTRY_FILENAME = "registry.json"
LOCK_FILENAME = "registry.lock"
LOGS_DIRNAME = "logs"

TERMINAL_STATUSES = {"finished", "failed", "crashed"}

#: A ``launching`` entry (placeholder inserted before the child's pid is known)
#: counts as live for this many seconds. Beyond that, a launch that never
#: attached a pid is treated as dead so it cannot block future launches.
LAUNCH_GRACE_SECONDS = 120


# -- /proc liveness helpers --------------------------------------------------

def _proc_start_time(pid):
    """Return the process start-time (field 22 of ``/proc/<pid>/stat``) or None.

    Field 22 (``starttime``, in clock ticks since boot) is the canonical
    discriminator for PID reuse: a reused pid has a different start-time.
    """
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    # The comm field (field 2) is wrapped in parens and may contain spaces or
    # ')'. Everything after the LAST ')' is space-separated starting at field 3.
    rparen = data.rfind(")")
    if rparen == -1:
        return None
    rest = data[rparen + 1:].split()
    # rest[0] is field 3 (state); field 22 is index 22 - 3 = 19.
    if len(rest) <= 19:
        return None
    return rest[19]


def _proc_cmdline(pid):
    """Return the process cmdline as a list of args, or None if unreadable."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return [a.decode("utf-8", "replace") for a in raw.split(b"\x00") if a]


def _pid_alive(pid):
    """Return True if a process with ``pid`` currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def local_process_alive(entry):
    """Return True if the local process behind ``entry`` is still the one we launched.

    Verifies pid liveness, the recorded start-time, and that the cmdline still
    carries this campaign's ``--campaign-id`` — all three must match.
    """
    pid = entry.get("pid")
    if not pid:
        return False
    if not _pid_alive(pid):
        return False
    recorded_start = entry.get("proc_start_time")
    if recorded_start is not None and _proc_start_time(pid) != recorded_start:
        return False  # pid reused by an unrelated process
    campaign_id = entry.get("campaign_id")
    cmdline = _proc_cmdline(pid)
    if campaign_id and cmdline is not None and campaign_id not in cmdline:
        return False
    return True


def _launching_is_fresh(entry, now_ts):
    """Return True if a placeholder ``launching`` entry is still within its grace."""
    started_at = entry.get("started_at")
    if not started_at:
        return False
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return False
    return (now_ts - started).total_seconds() < LAUNCH_GRACE_SECONDS


class CampaignRegistry:
    """File-backed, flock-guarded registry of launched campaigns."""

    def __init__(self, results_dir, classify_dead_local=None, now_fn=None):
        """Create a registry rooted at ``<results_dir>/_control``.

        Args:
            results_dir: The RoboVAST results directory.
            classify_dead_local: Optional ``entry -> "finished"|"crashed"`` used
                to classify a dead local entry during reconciliation. Defaults
                to always ``"crashed"``.
            now_fn: Optional ``() -> datetime`` clock injection (for tests).
        """
        self.control_dir = Path(results_dir) / CONTROL_DIRNAME
        self.registry_path = self.control_dir / REGISTRY_FILENAME
        self.lock_path = self.control_dir / LOCK_FILENAME
        self.logs_dir = self.control_dir / LOGS_DIRNAME
        self._classify_dead_local = classify_dead_local or (lambda _entry: "crashed")
        self._now = now_fn or datetime.now

    # -- paths ---------------------------------------------------------------

    def ensure_dirs(self):
        """Create ``_control`` and ``_control/logs`` if missing."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def log_path_for(self, campaign_id):
        """Return the stable log path for a local campaign's child process."""
        return self.logs_dir / f"{campaign_id}.log"

    # -- locked IO -----------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self):
        """Hold an exclusive advisory lock for a read-modify-write.

        Kept for the fast in-memory update + atomic rename only — never across a
        subprocess launch or a ``kubectl`` call.
        """
        self.ensure_dirs()
        with open(self.lock_path, "w", encoding="utf-8") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def _read_unlocked(self):
        """Read the registry mapping (call under ``_locked`` for consistency)."""
        try:
            with open(self.registry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_unlocked(self, data):
        """Atomically replace the registry file (temp file in ``_control`` + rename)."""
        self.ensure_dirs()
        fd, tmp = tempfile.mkstemp(dir=str(self.control_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp, self.registry_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # -- reconciliation ------------------------------------------------------
    #
    # Both backends are process-backed: a local campaign is a detached
    # ``vast exec local run`` child, a cluster campaign a detached
    # ``vast exec cluster run --wait-and-download`` child. Liveness is therefore
    # uniform (``local_process_alive``); only the single-flight guard is
    # backend-specific (local-only), because concurrent local runs collide on
    # shared Docker resources while concurrent cluster runs do not.

    def _reconcile(self, data):
        """Move dead process-backed entries to a terminal status (mutates ``data``)."""
        now_ts = self._now()
        for entry in data.values():
            status = entry.get("status")
            if status in TERMINAL_STATUSES:
                continue
            if status == "launching":
                # No pid yet; live only within the launch grace window.
                if not _launching_is_fresh(entry, now_ts):
                    entry["status"] = "crashed"
                    entry["finished_at"] = now_ts.isoformat()
                continue
            if not local_process_alive(entry):
                entry["status"] = self._classify_dead_local(entry)
                entry.setdefault("finished_at", now_ts.isoformat())
        return data

    def _live(self, data, backend=None):
        """Return live entries in ``data``, optionally filtered by backend."""
        now_ts = self._now()
        live = []
        for entry in data.values():
            if backend is not None and entry.get("backend") != backend:
                continue
            status = entry.get("status")
            if status in TERMINAL_STATUSES:
                continue
            if status == "launching":
                if _launching_is_fresh(entry, now_ts):
                    live.append(entry)
            elif local_process_alive(entry):
                live.append(entry)
        return live

    # -- public API ----------------------------------------------------------

    def reserve_local(self, *, campaign_id, config_filter, runs, expected_total,
                      log_path):
        """Atomically check local single-flight and insert a launching placeholder.

        Returns ``(True, None)`` and records a ``launching`` entry if no local
        campaign is live; otherwise ``(False, running_entries)`` and records
        nothing. Call :meth:`attach_pid` once the child is spawned.
        """
        return self._reserve(campaign_id=campaign_id, backend="local",
                             config_filter=config_filter, runs=runs,
                             expected_total=expected_total, log_path=log_path,
                             single_flight=True)

    def reserve_cluster(self, *, campaign_id, config_filter, runs, expected_total,
                        log_path, context=""):
        """Insert a launching placeholder for a cluster campaign (no single-flight)."""
        return self._reserve(campaign_id=campaign_id, backend="cluster",
                             config_filter=config_filter, runs=runs,
                             expected_total=expected_total, log_path=log_path,
                             single_flight=False, extra={"context": context})

    def _reserve(self, *, campaign_id, backend, config_filter, runs, expected_total,
                log_path, single_flight, extra=None):
        with self._locked():
            data = self._reconcile(self._read_unlocked())
            if single_flight:
                live = self._live(data, backend="local")
                if live:
                    return False, [dict(e) for e in live]
            entry = {
                "campaign_id": campaign_id,
                "backend": backend,
                "pid": None,
                "proc_start_time": None,
                "config_filter": config_filter,
                "runs": runs,
                "expected_total": expected_total,
                "log_path": str(log_path),
                "started_at": self._now().isoformat(),
                "exit_code": None,
                "finished_at": None,
                "status": "launching",
            }
            if extra:
                entry.update(extra)
            data[campaign_id] = entry
            self._write_unlocked(data)
            return True, None

    def attach_pid(self, campaign_id, pid, proc_start_time):
        """Record the child's pid and mark the entry ``running``."""
        self.update(campaign_id, pid=pid, proc_start_time=proc_start_time,
                    status="running")

    def update(self, campaign_id, **fields):
        """Merge ``fields`` into an entry and persist. Returns the entry or None."""
        with self._locked():
            data = self._read_unlocked()
            entry = data.get(campaign_id)
            if entry is None:
                return None
            entry.update(fields)
            self._write_unlocked(data)
            return dict(entry)

    def remove(self, campaign_id):
        """Delete an entry if present."""
        with self._locked():
            data = self._read_unlocked()
            if data.pop(campaign_id, None) is not None:
                self._write_unlocked(data)

    def get(self, campaign_id):
        """Return a copy of an entry (no reconciliation), or None."""
        entry = self._read_unlocked().get(campaign_id)
        return dict(entry) if entry else None

    def reconcile_and_get(self, campaign_id):
        """Reconcile process liveness, persist, and return the entry (or None)."""
        with self._locked():
            data = self._reconcile(self._read_unlocked())
            self._write_unlocked(data)
            entry = data.get(campaign_id)
            return dict(entry) if entry else None

    def entries(self, reconcile=True):
        """Return all entries, optionally reconciling process liveness first."""
        if not reconcile:
            return [dict(e) for e in self._read_unlocked().values()]
        with self._locked():
            data = self._reconcile(self._read_unlocked())
            self._write_unlocked(data)
            return [dict(e) for e in data.values()]

    def live_entries(self, backend=None):
        """Return reconciled live entries (persists reconciliation).

        Both local and cluster campaigns are process-backed, so this reports
        either backend by default; pass ``backend`` to filter.
        """
        with self._locked():
            data = self._reconcile(self._read_unlocked())
            self._write_unlocked(data)
            return [dict(e) for e in self._live(data, backend=backend)]
