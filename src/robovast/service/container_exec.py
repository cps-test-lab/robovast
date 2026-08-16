"""Run one command in an experiment image — the lane-agnostic half.

This is a **diagnostic**: it answers "is this container set up correctly?" and "does
this one config run?" without producing a campaign. Nothing it does is durable, and
that is enforced structurally rather than by convention — no campaign directory is
created, ``/out`` is never mounted, and ``OUTPUT_DIR`` points inside the container so
it dies with it. A caller cannot accidentally mistake its output for a result.

What lives here: request validation, limit derivation, the staging that turns a project
into a mountable ``/config``, the environment the command runs under, and the
single-container state machine plus its reaper. What lives in a transport: actually
starting a container and exec'ing in it, via :class:`ExecLane`.

Two invariants worth stating, because both were deliberate choices:

- **At most one exec container exists at a time**, named :data:`CONTAINER_NAME`. That
  removes session ids, a listing operation, and the whole leak class — a stray one is
  found and reaped by name. It also must *not* be the campaign container's name, which
  is single-flight and force-removed by ``LocalTransport`` to unblock a stop.
- **The command runs through the run's own ``entrypoint.sh``**, never a hand-rolled
  prelude. The environment a scenario sees (the ROS overlay, ``/ws/install``, the
  init block, ``execution.env``, ``PRE_COMMAND``) is defined there and will grow; a
  diagnostic that reconstructed it would drift and answer a different question than
  the run.
"""

import logging
import os
import shutil
import tempfile
import threading
import time
from typing import Optional, Protocol

from robovast.common.execution import prepare_campaign_configs, render_entrypoint, scenario_env
from robovast.common.host_display import host_display
from robovast.service.interface import ExecContainerState, ExecRequest, ExecResult, ExecStopResult

logger = logging.getLogger(__name__)

#: Deliberately not ``robovast``: that is the campaign container's single-flight name,
#: and ``LocalTransport`` runs ``docker rm -f robovast`` to unblock a stop — which would
#: kill a held diagnostic container, or be blocked by it.
CONTAINER_NAME = "robovast-exec"
#: Cluster equivalent, for finding and reaping strays.
POD_LABEL = "app=robovast-exec"

#: Everything the entrypoint writes goes here: inside the container, so it dies with it.
#: ``entrypoint.sh`` needs *somewhere* writable — it runs ``mkdir -p``/``tee`` under
#: ``set -e`` before anything else — and that somewhere must not be ``/out``.
OUTPUT_DIR = "/tmp/robovast-exec"

#: A command gets a fixed cap; anything needing longer wants a campaign. Defined with the
#: address space in ``service/interface.py``, because a client sizes its read timeout by
#: the same number and must not import the server to learn it.
from robovast.service.interface import COMMAND_LIMIT_S

#: Used when a scenario is asked for but the project sets no ``execution.timeout``. The
#: cluster's 1-hour campaign fallback is deliberately not inherited: an hour of life for
#: a diagnostic container is a leak, not a limit.
DEFAULT_SCENARIO_LIMIT_S = 300
#: Reaped this long after the last command — but only while nothing we started is
#: running, or a scenario would be killed while it worked.
IDLE_REAP_S = 60
#: Ceiling on idle waiting, so a held container cannot be kept alive indefinitely by a
#: caller that pokes it every 59 seconds.
IDLE_WAIT_CAP_S = 300
#: Added to a workload's own limit before the hard stop, so the workload hits its
#: timeout first and reports it, rather than vanishing with the container.
DEADLINE_GRACE_S = 30

LIMIT_SOURCE_COMMAND = "command"
LIMIT_SOURCE_CONFIG = "execution.timeout"
LIMIT_SOURCE_DEFAULT = "default"


class ExecLane(Protocol):
    """The lane-specific half: one container, started and exec'd into.

    Implemented by ``LocalTransport`` (docker) and ``ClusterService`` (an aux pod).
    """

    def run_once(self, spec: "ExecSpec", limit_s: int) -> tuple[int, str, str, bool]:
        """Run the command in a throwaway container. Returns exit/stdout/stderr/timed_out."""

    def start_held(self, spec: "ExecSpec", deadline_s: int) -> None:
        """Start the held container, idle, so commands can be exec'd into it."""

    def exec_in_held(self, spec: "ExecSpec", limit_s: int,
                     detach: bool) -> tuple[int, str, str, bool]:
        """Run the command inside the held container."""

    def stop_held(self) -> bool:
        """Stop the held container. True if one was there."""

    def held_workload_running(self) -> bool:
        """True if anything besides the container's idle PID 1 is still running."""


class ExecSpec:
    """Everything a lane needs to run one command: image, mounts, env, argv.

    ``config_dir`` is a staging directory this object owns — the caller must
    :meth:`close` it (or use it as a context manager) so a failed exec does not leak a
    temp tree.
    """

    def __init__(self, *, image: str, command: str, config_dir: str,
                 env: dict, workspace_dir: str = "", workspace_id: str = "",
                 config_name: str = "", log_path: str = "", staging_dir: str = "",
                 gui: bool = False):
        self.image = image
        self.command = command
        #: Host directory mounted read-only at ``/config`` — already in final layout.
        self.config_dir = config_dir
        self.env = env
        self.workspace_dir = workspace_dir
        self.workspace_id = workspace_id
        self.config_name = config_name
        self.log_path = log_path
        #: Mount the host's X socket so the container can draw on the serve host's
        #: display. A lane property rather than an env one: the mount exists only from
        #: container creation, which is why it is part of the held container's identity.
        self.gui = gui
        #: The temp tree that owns ``config_dir``; removed by :meth:`close`.
        self._staging_dir = staging_dir or config_dir

    @property
    def runs_scenario(self) -> bool:
        """True when the entrypoint is invoked with no argv, i.e. runs the scenario."""
        return not self.command

    def entrypoint_argv(self) -> list[str]:
        """The argv handed to the container.

        ``entrypoint.sh`` with no arguments *is* "run this config as the campaign
        would"; with arguments it sets the environment up and then ``exec``s them. The
        ``bash -c`` form also matters for output capture: the entrypoint skips its
        stdout redirection precisely when the argv mentions a shell, so a command's
        output reaches the caller instead of a log file inside the container.
        """
        argv = ["/config/entrypoint.sh"]
        if self.command:
            argv += ["bash", "-c", self.command]
        return argv

    def detached_start_script(self) -> str:
        """Shell that starts this spec's scenario in the background and proves it lives.

        Shared by both lanes, and that sharing is the point: the two had their own copies,
        and the fix for a silent failure — a scenario that died on launch while the exec
        reported success — landed in only one of them.

        Three things it must do:

        - run the entrypoint via ``bash <script>``. The staged file is written 0644, so
          invoking it directly fails on the missing exec bit, and in the detached form that
          failure is invisible because the child dies after the exec already returned 0.
        - ``setsid`` it, so it leaves this exec's session and survives into the next call.
        - **verify it is still alive**, and surface the log tail when it is not. Starting is
          not the same as running, and the difference must not be silent.
        """
        import shlex
        argv = " ".join(shlex.quote(a) for a in ["/bin/bash"] + self.entrypoint_argv())
        log = self.log_path or f"{OUTPUT_DIR}/logs/system.log"
        quoted_log = shlex.quote(log)
        return (
            f"mkdir -p {shlex.quote(os.path.dirname(log))}; "
            f"setsid nohup {argv} >> {quoted_log} 2>&1 & "
            'pid=$!; sleep 1; '
            'if kill -0 "$pid" 2>/dev/null; then echo "$pid"; else '
            f"echo 'the scenario exited immediately; last lines of {log}:' >&2; "
            f"tail -20 {quoted_log} >&2; exit 1; fi")

    def foreground_argv(self) -> list:
        """Argv for a command whose output is the answer — also via ``bash <script>``."""
        return ["/bin/bash"] + self.entrypoint_argv()

    def close(self) -> None:
        """Remove the staging tree. Idempotent.

        Not a context manager on purpose: a held container keeps this mounted as
        ``/config``, so the lifetime belongs to :class:`ContainerExecManager` rather than
        to the call that built the spec — a ``with`` block here would unmount it from
        under a container that is still running.
        """
        if self._staging_dir and os.path.isdir(self._staging_dir):
            shutil.rmtree(self._staging_dir, ignore_errors=True)
        self._staging_dir = ""
        self.config_dir = ""


def validate(request: ExecRequest) -> None:
    """Reject a request that cannot be honoured, before any container starts.

    Every rule here refuses rather than guesses. Resolving an omitted source from the
    held container was specifically avoided: the same fallback elsewhere in this service
    once ran a different ``.vast`` than the caller named.
    """
    has_workspace = bool(request.workspace_id)
    has_campaign = bool(request.campaign_id)
    if has_workspace and has_campaign:
        raise ValueError(
            "name one source, not both: workspace_id (+config_path) or campaign_id")
    if not has_workspace and not has_campaign:
        raise ValueError(
            "no source named: pass workspace_id (+config_path) for a workspace project, "
            "or campaign_id to use an existing campaign's _config/ as the project. "
            "A source is required on every call, including follow-ups against a held "
            "container")
    if request.config_path and not has_workspace:
        raise ValueError("config_path names a .vast inside a workspace; it needs workspace_id")
    if not request.command.strip() and not request.config_name:
        raise ValueError(
            "nothing to run: an empty command means 'run the staged config's scenario' "
            "and needs config_name; otherwise pass a command")


def derive_limit(campaign_data: Optional[dict], command: str) -> tuple[int, str]:
    """How long this may run, and which rule decided.

    Reporting the source is what makes a truncation self-explaining: hitting 300 s
    because commands get 300 s and hitting it because the project set no
    ``execution.timeout`` call for different reactions from the caller.
    """
    if command.strip():
        return COMMAND_LIMIT_S, LIMIT_SOURCE_COMMAND
    timeout = ((campaign_data or {}).get("execution") or {}).get("timeout")
    if timeout:
        return int(timeout), LIMIT_SOURCE_CONFIG
    return DEFAULT_SCENARIO_LIMIT_S, LIMIT_SOURCE_DEFAULT


def deadline_for(limit_s: int) -> int:
    """Hard stop for a held container running a workload with *limit_s*.

    Clamping this to a flat 300 s would silently truncate a project that set
    ``timeout: 900`` — ignoring a value the caller did supply.
    """
    return max(IDLE_WAIT_CAP_S, limit_s + DEADLINE_GRACE_S)


def build_env(scenario_vars: dict, execution: dict, *, staged_config: bool,
              gui: bool = False) -> dict:
    """The environment the command runs under.

    Reuses the run's own derivation (:func:`~robovast.common.scenario_env`) so the
    diagnostic sees what the scenario sees, and overrides only what must differ for a
    container that produces nothing durable.
    """
    env = dict(scenario_vars)
    env["OUTPUT_DIR"] = OUTPUT_DIR
    env["SCENARIO_OUTPUT_DIR"] = OUTPUT_DIR
    # SCENARIO_PARAMETER_FILE is deliberately left alone: a campaign stages the single
    # config's parameters at ``<config>/_config/scenario.config``, and the assembled
    # mount puts them at ``/config/scenario.config`` — already the entrypoint's default.
    # No *virtual* framebuffer: Xvfb costs seconds we exist to save, and a command that
    # needs one belongs in a campaign. This holds with `gui` too — there the container
    # draws on the host's X server through a mounted socket, which costs nothing and is
    # precisely what Xvfb would shadow.
    env["ENABLE_X11"] = "false"
    if gui:
        # The socket is mounted by the lane; this says which display to use. Read from
        # the service process, defaulting like the generated compose does, so a daemon
        # started without DISPLAY still reaches a running :0.
        env["DISPLAY"] = host_display() or ":0"
        env.setdefault("LIBGL_ALWAYS_SOFTWARE",
                       os.environ.get("LIBGL_ALWAYS_SOFTWARE", "0"))
    if not staged_config:
        # Nothing is staged in the bare-image case, so /config/collect_sysinfo.py does
        # not exist — and under `set -e` the entrypoint would abort on it before ever
        # running the requested command.
        env["COLLECT_SYSINFO"] = "false"
    extra = execution.get("env")
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, dict):
                env.update({str(k): str(v) for k, v in item.items()})
    pre_command = execution.get("pre_command")
    if pre_command:
        env["PRE_COMMAND"] = str(pre_command)
    params = execution.get("scenario_execution_parameters")
    if params:
        env["SCENARIO_EXECUTION_PARAMETERS"] = str(params)
    return env


def stage(vast_file: str, config_name: str, *,
          cluster: bool, command: str,
          gui: bool = False) -> tuple[ExecSpec, dict, int, str]:
    """Turn a resolved ``.vast`` into a runnable :class:`ExecSpec`.

    A campaign's ``_config/`` is itself a project, so both sources reach this with just
    a path and take one code path from here. Returns the spec, the campaign data (for
    the caller's image resolution), and the derived limit.

    The entrypoint is always rendered **for the lane this exec runs on** — never copied
    from a campaign. ``prepare_campaign_configs`` substitutes lane-specific init and
    post-run blocks, so a cluster campaign's entrypoint carries cluster init and
    S3-mirroring logic that would be wrong to run locally.
    """
    from robovast.common import load_config
    from robovast.execution.controller import build_campaign_data, filter_configs_by_name

    staging = tempfile.mkdtemp(prefix="robovast_exec_")
    try:
        generated = os.path.join(staging, "generated")
        if not config_name:
            # Bare image: only the entrypoint is needed, so no config tree is expanded.
            # That keeps a "does this import?" check off the variation-plugin path, and
            # avoids failing input checks (a missing .osc) that the question does not
            # depend on.
            execution = (load_config(vast_file) or {}).get("execution") or {}
            campaign_data = {"configs": [], "execution": execution}
            os.makedirs(os.path.join(generated, "_transient"), exist_ok=True)
            with open(os.path.join(generated, "_transient", "entrypoint.sh"),
                      "w", encoding="utf-8") as f:
                f.write(render_entrypoint(cluster=cluster))
            scenario_vars = {}
        else:
            campaign_data, _transient = build_campaign_data(vast_file, generated)
            campaign_data["configs"] = filter_configs_by_name(
                campaign_data["configs"], config_name)
            # gui is forwarded so ``execution.local.gui.parameter_overrides`` reaches the
            # staged scenario: without it a windowed exec would stage the headless
            # defaults and draw nothing on the display it just mounted.
            prepare_campaign_configs(generated, campaign_data, cluster=cluster, gui=gui)
            scenario_vars = scenario_env(campaign_data)
        execution = campaign_data.get("execution") or {}
        config_mount = _assemble_config_mount(staging, generated, campaign_data)
        limit_s, limit_source = derive_limit(campaign_data, command)
        env = build_env(scenario_vars, execution, staged_config=bool(config_name),
                        gui=gui)
        spec = ExecSpec(
            image="", command=command, config_dir=config_mount, env=env,
            staging_dir=staging, config_name=config_name, gui=gui,
            log_path=f"{OUTPUT_DIR}/logs/system.log" if not command.strip() else "")
        return spec, campaign_data, limit_s, limit_source
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _assemble_config_mount(staging: str, generated: str, campaign_data: dict) -> str:
    """Lay out one directory that *is* the container's ``/config``.

    A campaign reaches the same state through a dozen individual bind mounts
    (``_transient/entrypoint.sh``, the ``.osc``, each run file, the per-config
    parameter file). Assembling the tree once and mounting it once is the same
    result with one thing to verify instead of twelve, and it keeps the layout in a
    place a test can read.

    The one subtlety is the parameter file: a campaign stages it per config at
    ``<config>/_config/scenario.config``, which is exactly where ``entrypoint.sh``
    looks by default — so a single staged config needs no ``SCENARIO_PARAMETER_FILE``
    override at all.
    """
    mount = os.path.join(staging, "config")
    os.makedirs(mount, exist_ok=True)
    transient = os.path.join(generated, "_transient")
    for name in ("entrypoint.sh", "collect_sysinfo.py", "monitor_resources.py"):
        src = os.path.join(transient, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(mount, name))
    shared = os.path.join(generated, "_config")
    if os.path.isdir(shared):
        # The scenario, plus run_files (``files/*.py`` and friends) at the same relative
        # paths the .osc refers to them by. The project's own .vast is copied along with
        # them, harmlessly — a campaign mounts it too.
        for entry in os.listdir(shared):
            src = os.path.join(shared, entry)
            dst = os.path.join(mount, entry)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    for config in campaign_data.get("configs") or []:
        per_config = os.path.join(generated, config.get("name", ""), "_config")
        if os.path.isdir(per_config):
            shutil.copytree(per_config, mount, dirs_exist_ok=True)
    return mount


def vast_in_dir(project_dir: str, config_path: str = "") -> str:
    """The ``.vast`` inside *project_dir*, refusing ambiguity rather than picking one.

    For the campaign source, whose project dir is a campaign's ``_config/``. The
    workspace source does not come through here — the transport's own workspace
    resolution already answers this, and duplicating it would be a second place for the
    multi-``.vast`` rule to drift.
    """
    if not os.path.isdir(project_dir):
        raise ValueError(f"no project directory at {project_dir}")
    if config_path:
        candidate = os.path.join(project_dir, config_path)
        if not os.path.isfile(candidate):
            raise ValueError(f"no such .vast in the project: {config_path}")
        return candidate
    found = sorted(f for f in os.listdir(project_dir) if f.endswith(".vast"))
    if not found:
        raise ValueError(f"no .vast file in {project_dir}")
    if len(found) > 1:
        raise ValueError(
            "the project holds several .vast files, so config_path must name one: "
            + ", ".join(found))
    return os.path.join(project_dir, found[0])


class ContainerExecManager:
    """Owns the single container's lifetime; delegates container work to an :class:`ExecLane`.

    Threading: one lock guards the held-container record. The reaper runs on its own
    thread and takes the same lock, so a reap cannot race a call that is about to reuse
    the container.
    """

    def __init__(self, lane: ExecLane, *, poll_s: float = 5.0):
        self._lane = lane
        self._poll_s = poll_s
        self._lock = threading.RLock()
        self._held: Optional[dict] = None
        self._reaper: Optional[threading.Thread] = None
        self._stop_reaper = threading.Event()

    # -- state reported to callers ---------------------------------------

    def state(self) -> Optional[ExecContainerState]:
        """The held container as a caller sees it, or ``None`` when nothing is held."""
        with self._lock:
            if not self._held:
                return None
            held = self._held
            running = self._workload_running_locked()
            idle_in = None
            if not running:
                idle_in = max(0, int(held["idle_deadline"] - time.monotonic()))
            return ExecContainerState(
                kept=True, reused=held["reused"], image=held["image"],
                config=held["config"], idle_expires_in_s=idle_in,
                deadline_in_s=max(0, int(held["deadline"] - time.monotonic())))

    # -- the two operations ----------------------------------------------

    def run(self, spec: ExecSpec, limit_s: int, *, keep_alive: bool,
            identity: tuple) -> tuple[int, str, str, bool]:
        """Run *spec*, holding the container afterwards when asked.

        Takes ownership of *spec*'s staging directory: a held container bind-mounts it
        as ``/config``, so it must outlive the call that created it and be removed when
        the container is — not when the call returns.
        """
        if not keep_alive:
            # One-shot means a clean container, so a previously held one goes first —
            # otherwise "one-shot" would quietly inherit whatever state was left there.
            self.stop()
            try:
                return self._lane.run_once(spec, limit_s)
            finally:
                spec.close()

        reused = self._ensure_held(spec, limit_s, identity)
        with self._lock:
            if self._held:
                self._held["reused"] = reused
        # A scenario is detached so this call can return and the next one inspect it;
        # a command runs in the foreground and its output is the answer.
        result = self._lane.exec_in_held(spec, limit_s, detach=spec.runs_scenario)
        self._touch()
        return result

    def stop(self) -> ExecStopResult:
        """Stop the held container. Nothing held is an empty result, not a failure.

        The lane's answer counts even when this manager has no record: a container can
        outlive the record (a service restart), and reaping that stray is the point of
        giving it a fixed name.
        """
        with self._lock:
            had_record = self._held is not None
        self._stop_reaper.set()
        # Container first, then its /config: unmounting by removing the host directory
        # under a live container would be the wrong order.
        stopped = bool(self._lane.stop_held()) or had_record
        self._release_held_spec()
        with self._lock:
            self._held = None
        return ExecStopResult(stopped=stopped,
                              target=CONTAINER_NAME if stopped else None)

    def _release_held_spec(self) -> None:
        """Drop the staging tree the held container had mounted."""
        with self._lock:
            spec = (self._held or {}).get("spec")
        if spec is not None:
            spec.close()

    # -- internals -------------------------------------------------------

    def _ensure_held(self, spec: ExecSpec, limit_s: int, identity: tuple) -> bool:
        """Start, reuse, or replace the held container. True if reused."""
        with self._lock:
            held = self._held
            if held and held["identity"] == identity:
                # The live container already has the right /config mounted; this call's
                # freshly staged copy is redundant.
                spec.close()
                return True
            if held:
                # Replacing would silently kill whatever is running in there — a
                # destructive act inferred from a changed argument rather than asked
                # for. Refuse and name the way through.
                if self._workload_running_locked():
                    raise ValueError(
                        "a container is held for a different source and something is "
                        "still running in it; call stop_container first if you mean to "
                        "discard it")
        # Replace an idle container: nothing to lose.
        self._release_held_spec()
        self._lane.stop_held()
        deadline = deadline_for(limit_s)
        self._lane.start_held(spec, deadline)
        with self._lock:
            now = time.monotonic()
            self._held = {
                "identity": identity, "image": spec.image, "config": spec.config_name,
                "reused": False, "started": now, "idle_deadline": now + IDLE_REAP_S,
                "deadline": now + deadline,
                # Kept so the mounted /config outlives this call and is removed with
                # the container.
                "spec": spec,
            }
        self._start_reaper()
        return False

    def _touch(self) -> None:
        with self._lock:
            if self._held:
                self._held["idle_deadline"] = min(
                    time.monotonic() + IDLE_REAP_S,
                    self._held["started"] + IDLE_WAIT_CAP_S)

    def _workload_running_locked(self) -> bool:
        try:
            return self._lane.held_workload_running()
        except Exception as exc:            # a probe failure must not reap a live run
            logger.debug("could not probe %s workload: %s", CONTAINER_NAME, exc)
            return True

    def _start_reaper(self) -> None:
        if self._reaper and self._reaper.is_alive():
            return
        self._stop_reaper.clear()
        self._reaper = threading.Thread(target=self._reap_loop, name="exec-reaper",
                                        daemon=True)
        self._reaper.start()

    def _reap_loop(self) -> None:
        while not self._stop_reaper.wait(self._poll_s):
            with self._lock:
                held = self._held
                if not held:
                    return
                now = time.monotonic()
                if now >= held["deadline"]:
                    reason = "hard deadline reached"
                elif not self._workload_running_locked() and now >= held["idle_deadline"]:
                    reason = "idle"
                else:
                    continue
            logger.info("reaping exec container %s (%s)", CONTAINER_NAME, reason)
            self.stop()
            return


def result_from(exec_out: tuple[int, str, str, bool], *, spec: ExecSpec,
                limit_s: int, limit_source: str, duration_s: float,
                container: Optional[ExecContainerState]) -> ExecResult:
    """Assemble the response, including where a scenario's output actually went."""
    exit_code, stdout, stderr, timed_out = exec_out
    return ExecResult(
        exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out,
        duration_s=round(duration_s, 3), limit_s=limit_s, limit_source=limit_source,
        log_path=spec.log_path,
        container=container or ExecContainerState())


__all__ = [
    "CONTAINER_NAME", "POD_LABEL", "OUTPUT_DIR", "COMMAND_LIMIT_S",
    "DEFAULT_SCENARIO_LIMIT_S", "IDLE_REAP_S", "IDLE_WAIT_CAP_S",
    "LIMIT_SOURCE_COMMAND", "LIMIT_SOURCE_CONFIG", "LIMIT_SOURCE_DEFAULT",
    "ExecLane", "ExecSpec", "ContainerExecManager",
    "validate", "derive_limit", "deadline_for", "build_env", "stage", "result_from",
    "vast_in_dir",
]
