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

"""In-controller HTTP/JSON control channel (state + RPC).

Every cluster campaign is driven by a fire-and-forget **controller pod** (see
:mod:`robovast.execution.cluster_execution.controller_launcher`). The controller
loop deletes each batch's Kubernetes Jobs once their results are downloaded, so a
client that reconstructs progress purely from live Jobs cannot tell the gap
*between* search generations from the real end of the campaign, nor see the
loop-level state (current batch, search budget, run-level progress).

This module gives the controller a tiny **FastAPI + uvicorn** server, run on a
daemon thread beside the synchronous controller loop:

* ``GET  /status``  — the controller's live :class:`Status` (loop phase, current
  batch, budget progress, per-batch run progress, history). The CLI ``monitor``
  polls this; the ``phase`` field is the authoritative "done" signal.
* ``POST /command`` — an extensible RPC: dispatch ``{name, args}`` through the
  :data:`HANDLERS` registry. Ships one handler (``stop``); register more later.
* ``GET  /healthz`` — liveness.

FastAPI auto-emits an OpenAPI schema (``/docs``), so the same contract serves the
CLI now and a web UI later (reached via ``kubectl port-forward`` now, a Service /
Ingress later — no code change).

``fastapi`` / ``uvicorn`` are imported lazily (only :func:`build_app` /
:func:`serve_in_thread` need them) so the models and :class:`ControllerState`
import cleanly anywhere; ``pydantic`` is a core dependency.
"""

import logging
import threading
import time
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8099


# -- wire models ------------------------------------------------------------

class RunProgress(BaseModel):
    """Completed vs expected per-run artifacts for the current batch."""
    completed: int = 0
    total: int = 0


class BudgetItem(BaseModel):
    """One budget/stopping criterion's current value vs its limit."""
    label: str
    current: Optional[float] = None      # None when not-yet-defined (e.g. NaN)
    limit: float
    done: bool = False


class Status(BaseModel):
    """The controller's live state, served by ``GET /status``.

    ``phase`` is an **open** string the controller advances through a documented
    vocabulary (``starting`` → ``running`` → ``finishing`` → ``finished`` /
    ``failed``); ``stage`` and ``extra`` exist so future markers (e.g.
    ``"upload-to-share-done"``) slot in without a schema change.
    """
    # validate_assignment so the controller can assign plain dicts to the typed
    # sub-fields (``runs``, ``budget``) and they coerce to the models.
    model_config = ConfigDict(validate_assignment=True)

    phase: str = "starting"
    stage: Optional[str] = None
    mode: Optional[str] = None
    campaign_id: Optional[str] = None
    batch: int = 0                       # current batch index (0-based)
    batches_done: int = 0
    budget: list[BudgetItem] = Field(default_factory=list)
    runs: RunProgress = Field(default_factory=RunProgress)
    best_objective: Optional[float] = None
    batch_history: list[dict] = Field(default_factory=list)
    stop: Optional[dict] = None          # {kind, reason} once the loop ends
    extra: dict = Field(default_factory=dict)
    updated_at: float = Field(default_factory=time.time)


class Command(BaseModel):
    """An RPC request: a registered handler name plus its keyword args."""
    name: str
    args: dict = Field(default_factory=dict)


class CommandResult(BaseModel):
    ok: bool
    result: Any = None
    error: Optional[str] = None


# -- shared state -----------------------------------------------------------

class ControllerState:
    """Thread-safe holder the controller writes and the server reads.

    The controller calls :meth:`update` / :meth:`set_phase` at each batch
    boundary (and :meth:`update` for run-level progress within a batch); the
    server thread reads a consistent :meth:`snapshot`. :meth:`request_stop` /
    :attr:`stop_requested` back the cooperative ``stop`` command.
    """

    def __init__(self, **initial):
        self._lock = threading.Lock()
        self._status = Status(**initial)
        self._stop_event = threading.Event()
        # Retrigger plumbing for the post-campaign upload-to-share step: the
        # control server signals, the controller's main thread performs the
        # upload. A `stop` request abandons the wait and terminates.
        self._retrigger_event = threading.Event()
        self._retrigger_overrides: dict = {}

    def snapshot(self) -> Status:
        with self._lock:
            return self._status.model_copy(deep=True)

    def update(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self._status, key, value)
            self._status.updated_at = time.time()

    def set_phase(self, phase: str, stage: Optional[str] = None) -> None:
        with self._lock:
            self._status.phase = phase
            if stage is not None:
                self._status.stage = stage
            self._status.updated_at = time.time()

    def request_stop(self) -> None:
        self._stop_event.set()
        # Wake a thread blocked in wait_for_retrigger so `stop` also abandons a
        # stuck post-campaign upload.
        self._retrigger_event.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    # -- upload-to-share retrigger -----------------------------------------

    def request_upload(self, overrides: Optional[dict] = None) -> None:
        """Ask the controller's main thread to (re)run upload-to-share.

        *overrides* are optional ``{ENV_VAR: value}`` credential corrections
        applied before the next attempt (the manual retrigger usually needs no
        args — the launch-time credentials are still in the pod).
        """
        with self._lock:
            self._retrigger_overrides = dict(overrides or {})
        self._retrigger_event.set()

    def wait_for_retrigger(self) -> tuple[str, dict]:
        """Block until an upload retrigger or a stop is requested.

        Returns ``("retrigger", overrides)`` to retry the upload, or
        ``("abandon", {})`` when a ``stop`` was requested (give up, terminate).
        """
        self._retrigger_event.wait()
        self._retrigger_event.clear()
        if self._stop_event.is_set():
            return "abandon", {}
        with self._lock:
            overrides = dict(self._retrigger_overrides)
            self._retrigger_overrides = {}
        return "retrigger", overrides


# -- command registry -------------------------------------------------------

# name -> handler(state, **args) -> result. Extend by decorating new handlers.
HANDLERS: dict[str, Callable[..., Any]] = {}


def register(name: str) -> Callable[[Callable], Callable]:
    def deco(fn: Callable) -> Callable:
        HANDLERS[name] = fn
        return fn
    return deco


@register("stop")
def _stop(state: ControllerState, **_args) -> dict:
    """Request a cooperative graceful stop.

    During the campaign loop this ends the search after the current batch; while
    the controller is waiting to retry a failed upload-to-share, it abandons the
    wait and terminates the controller.
    """
    state.request_stop()
    return {"stop_requested": True}


@register("upload-to-share")
def _upload_to_share(state: ControllerState, **args) -> dict:
    """(Re)run the post-campaign upload-to-share.

    Signals the controller's main thread to perform the upload; the actual work
    runs there (not on this request thread). Optional *args* are credential
    overrides (e.g. a corrected password) applied before the retry. Poll
    ``GET /status`` for the ``stage`` transition to ``uploaded`` /
    ``upload-failed``.
    """
    state.request_upload(args)
    return {"upload_requested": True}


def dispatch(state: ControllerState, command: Command) -> CommandResult:
    handler = HANDLERS.get(command.name)
    if handler is None:
        return CommandResult(ok=False, error=f"unknown command '{command.name}'")
    try:
        return CommandResult(ok=True, result=handler(state, **command.args))
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("command '%s' failed: %s", command.name, exc, exc_info=True)
        return CommandResult(ok=False, error=str(exc))


# -- server -----------------------------------------------------------------

def build_app(state: ControllerState):
    """Build the FastAPI app bound to *state* (lazy import; needs ``fastapi``)."""
    from fastapi import FastAPI, HTTPException  # pylint: disable=import-outside-toplevel

    app = FastAPI(title="robovast controller", docs_url="/docs")

    @app.get("/status", response_model=Status)
    def get_status() -> Status:
        return state.snapshot()

    @app.post("/command", response_model=CommandResult)
    def post_command(command: Command) -> CommandResult:
        result = dispatch(state, command)
        if not result.ok and (result.error or "").startswith("unknown command"):
            raise HTTPException(status_code=400, detail=result.error)
        return result

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    return app


def serve_in_thread(state: ControllerState, port: int = DEFAULT_PORT,
                    host: str = "0.0.0.0") -> threading.Thread:  # nosec B104 - in-cluster pod
    """Start the control server on a daemon thread; return the thread.

    Best-effort: a failure to start (e.g. ``uvicorn`` missing) is logged and the
    campaign proceeds without the channel — the monitor then falls back to its
    Kubernetes-only view.
    """
    def _run() -> None:
        try:
            import uvicorn  # pylint: disable=import-outside-toplevel
            app = build_app(state)
            uvicorn.Server(uvicorn.Config(
                app, host=host, port=port, log_level="warning")).run()
        except Exception:  # pylint: disable=broad-except
            logger.warning("Control server failed to start; monitor will fall back "
                           "to the Kubernetes-only view.", exc_info=True)

    thread = threading.Thread(target=_run, name="robovast-control-server", daemon=True)
    thread.start()
    logger.info("Control server listening on %s:%d (GET /status, POST /command).", host, port)
    return thread
