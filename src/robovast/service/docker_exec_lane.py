"""The local-Docker half of container exec: one container, run or exec'd into.

Mirrors the conventions the variation-plugin runner already proved
(:class:`~robovast.common.variation.container_runner.LocalContainerRunner`) —
``--rm``, an explicit ``--user``, an overridden entrypoint so the argv runs verbatim —
but captures output under a timeout instead of streaming it and raising.
"""

import logging
import os
import subprocess

from robovast.service.container_exec import CONTAINER_NAME, ExecSpec

logger = logging.getLogger(__name__)

#: Timeout for the short bookkeeping commands (run -d / top / rm). Generous, because
#: a slow daemon should surface as an error rather than as a false "nothing there".
_PROBE_TIMEOUT_S = 40


class DockerExecLane:
    """Runs exec commands on the local Docker daemon."""

    def run_once(self, spec: ExecSpec, limit_s: int) -> tuple[int, str, str, bool]:
        """Run in a throwaway container: nothing survives it, by construction."""
        cmd = ["docker", "run", "--rm", "--name", CONTAINER_NAME]
        cmd += self._common_run_args(spec)
        cmd += ["--entrypoint", "/bin/bash", spec.image]
        cmd += spec.entrypoint_argv()
        return _capture(cmd, limit_s)

    def start_held(self, spec: ExecSpec, deadline_s: int) -> None:
        """Start the container idle so later calls can exec into it.

        PID 1 is a plain sleep rather than the entrypoint: the entrypoint's job is to
        set up the environment for a command, and every ``docker exec`` re-runs it. A
        long-lived PID 1 also gives backgrounded processes something to reparent to, so
        a scenario started in one call survives into the next.
        """
        cmd = ["docker", "run", "-d", "--name", CONTAINER_NAME]
        cmd += self._common_run_args(spec)
        # A container-level backstop in case the service dies before its reaper runs:
        # sleep exits, the container stops, and nothing is left holding memory.
        cmd += ["--entrypoint", "/bin/bash", spec.image,
                "-c", f"exec sleep {int(deadline_s)}"]
        done = subprocess.run(cmd, capture_output=True, text=True,  # noqa: S603
                              timeout=_PROBE_TIMEOUT_S, check=False)
        if done.returncode != 0:
            raise RuntimeError(
                f"could not start exec container: {done.stderr.strip() or done.stdout.strip()}")

    def exec_in_held(self, spec: ExecSpec, limit_s: int,
                     detach: bool) -> tuple[int, str, str, bool]:
        """Run the command inside the held container.

        *detach* is for a scenario: it must keep running after this call returns so the
        next call can inspect it. ``setsid`` puts it in its own session, without which
        it would be torn down with the exec that started it.
        """
        cmd = ["docker", "exec"]
        for key, value in spec.env.items():
            cmd += ["-e", f"{key}={value}"]
        cmd.append(CONTAINER_NAME)
        if detach:
            cmd += ["/bin/bash", "-c", spec.detached_start_script()]
            return _capture(cmd, _PROBE_TIMEOUT_S)
        cmd += spec.foreground_argv()
        return _capture(cmd, limit_s)

    def stop_held(self) -> bool:
        """Remove the container. True when one was actually there.

        A failure that is *not* "no such container" is logged rather than swallowed: a
        stop that silently did nothing would leave a container holding memory while the
        service reported it gone.
        """
        try:
            done = subprocess.run(  # noqa: S603
                ["docker", "rm", "-f", CONTAINER_NAME],
                capture_output=True, text=True,
                timeout=_PROBE_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("could not remove %s: %s", CONTAINER_NAME, e)
            return False
        if done.returncode == 0:
            return bool(done.stdout.strip())
        stderr = (done.stderr or "").strip()
        if "No such container" not in stderr:
            logger.warning("removing %s failed: %s", CONTAINER_NAME, stderr)
        return False

    def held_workload_running(self) -> bool:
        """True if anything besides the container's idle PID 1 is running.

        Counts processes rather than matching names: ``docker top`` reports *host* PIDs,
        so the idle PID 1 cannot be picked out by number, and its command is ``sleep`` —
        indistinguishable from a ``sleep`` a caller backgrounded. One process means only
        the idle placeholder is left.

        Deliberately broader than "the scenario we started": a command that backgrounded
        something itself counts too, so the idle reaper cannot kill work it did not know
        about. Probe failures other than a missing container propagate — the manager
        treats an unanswerable probe as "still busy" rather than reaping a live run.
        """
        done = subprocess.run(  # noqa: S603
            ["docker", "top", CONTAINER_NAME, "-eo", "pid"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S, check=False)
        if done.returncode != 0:
            if "No such container" in (done.stderr or "") or "is not running" in (done.stderr or ""):
                # Gone already: saying so lets the manager drop a stale record.
                return False
            raise RuntimeError(f"could not inspect {CONTAINER_NAME}: {done.stderr.strip()}")
        # First line is the ps header.
        pids = [ln for ln in done.stdout.splitlines()[1:] if ln.strip().isdigit()]
        return len(pids) > 1

    # -- shared argv ------------------------------------------------------

    def _common_run_args(self, spec: ExecSpec) -> list:
        """Mounts, user and env shared by the one-shot and held forms."""
        args = ["--user", f"{os.getuid()}:{os.getgid()}"]
        # One mount, already in final layout (see _assemble_config_mount): the rendered
        # entrypoint, the monitor scripts, and — for a staged config — the scenario, its
        # run files and its parameter file. Read-only, like a campaign job's /config.
        args += ["-v", f"{spec.config_dir}:/config:ro"]
        if spec.workspace_dir and spec.workspace_id:
            # Mounted at its own file address, so a path from write_file is usable here
            # verbatim. Read-only: campaign inputs are not a diagnostic's to rewrite.
            args += ["-v", f"{spec.workspace_dir}:/sources/{spec.workspace_id}:ro"]
        for key, value in spec.env.items():
            args += ["-e", f"{key}={value}"]
        return args


def _capture(cmd: list, limit_s: int):
    """Run *cmd*, returning (exit_code, stdout, stderr, timed_out)."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,  # noqa: S603
                              timeout=max(1, int(limit_s)), check=False)
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        err = e.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return 124, out, err, True
    except OSError as e:
        raise RuntimeError(f"could not run docker: {e}") from e
    return done.returncode, done.stdout, done.stderr, False
