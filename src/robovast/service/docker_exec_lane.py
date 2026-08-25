"""The local-Docker half of container exec: one container, run or exec'd into.

Mirrors the conventions the variation-plugin runner already proved
(:class:`~robovast.common.variation.container_runner.LocalContainerRunner`) —
``--rm``, an explicit ``--user``, an overridden entrypoint so the argv runs verbatim —
but captures output under a timeout instead of streaming it and raising.
"""

import logging
import os
import subprocess

from robovast.common.host_display import grant_local_access
from robovast.service.container_exec import CONTAINER_NAME, SLOT_USER, ExecSpec, container_name

logger = logging.getLogger(__name__)

#: Timeout for the short bookkeeping commands (run -d / top / rm). Generous, because
#: a slow daemon should surface as an error rather than as a false "nothing there".
_PROBE_TIMEOUT_S = 40


class DockerExecLane:
    """Runs exec commands on the local Docker daemon."""

    def run_once(self, spec: ExecSpec, limit_s: int) -> tuple[int, str, str, bool]:
        """Run in a throwaway container: nothing survives it, by construction."""
        cmd = ["docker", "run", "--rm", "--name", container_name()]
        cmd += self._common_run_args(spec)
        cmd += ["--entrypoint", "/bin/bash", spec.image]
        cmd += spec.entrypoint_argv()
        return _capture(cmd, limit_s)

    def start_held(self, spec: ExecSpec, deadline_s: int,
                   slot: str = SLOT_USER) -> None:
        """Start the container idle so later calls can exec into it.

        PID 1 is a plain sleep rather than the entrypoint: the entrypoint's job is to
        set up the environment for a command, and every ``docker exec`` re-runs it. A
        long-lived PID 1 also gives backgrounded processes something to reparent to, so
        a scenario started in one call survives into the next.
        """
        cmd = ["docker", "run", "-d", "--name", container_name(slot)]
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

    def exec_in(self, target, argv: list, limit_s: int,
                env: dict | None = None) -> tuple[int, str, str, bool]:
        """``docker exec`` into *target*, which is a container name."""
        cmd = ["docker", "exec"]
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        cmd.append(target)
        cmd += list(argv)
        return _capture(cmd, limit_s)

    def exec_in_held(self, spec: ExecSpec, limit_s: int, detach: bool,
                     slot: str = SLOT_USER) -> tuple[int, str, str, bool]:
        """Run the command inside the held container.

        *detach* is for a scenario: it must keep running after this call returns so the
        next call can inspect it. ``setsid`` puts it in its own session, without which
        it would be torn down with the exec that started it.
        """
        if detach:
            return self.exec_in(container_name(slot),
                                ["/bin/bash", "-c", spec.detached_start_script()],
                                _PROBE_TIMEOUT_S, env=spec.env)
        return self.exec_in(container_name(slot), spec.foreground_argv(), limit_s,
                            env=spec.env)

    def stop_held(self, slot: str = SLOT_USER) -> bool:
        """Remove the container. True when one was actually there.

        A failure that is *not* "no such container" is logged rather than swallowed: a
        stop that silently did nothing would leave a container holding memory while the
        service reported it gone.
        """
        name = container_name(slot)
        try:
            done = subprocess.run(  # noqa: S603
                ["docker", "rm", "-f", name],
                capture_output=True, text=True,
                timeout=_PROBE_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("could not remove %s: %s", name, e)
            return False
        if done.returncode == 0:
            return bool(done.stdout.strip())
        stderr = (done.stderr or "").strip()
        if "No such container" not in stderr:
            logger.warning("removing %s failed: %s", name, stderr)
        return False

    def held_workload_running(self, slot: str = SLOT_USER) -> bool:
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
        name = container_name(slot)
        done = subprocess.run(  # noqa: S603
            ["docker", "top", name, "-eo", "pid"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S, check=False)
        if done.returncode != 0:
            if "No such container" in (done.stderr or "") or "is not running" in (done.stderr or ""):
                # Gone already: saying so lets the manager drop a stale record.
                return False
            raise RuntimeError(f"could not inspect {name}: {done.stderr.strip()}")
        # First line is the ps header.
        pids = [ln for ln in done.stdout.splitlines()[1:] if ln.strip().isdigit()]
        return len(pids) > 1

    def sweep_held(self) -> list:
        """Remove every ``robovast-exec*`` container. Returns the names removed.

        Prefix-matched rather than name-by-name because a query container's name carries a
        hash of the identity it was started for, and nothing persists those across a
        service restart. ``--filter name=`` is a regex, so the anchor is what keeps this
        from matching a user's container that merely starts with something similar.
        """
        try:
            listed = subprocess.run(  # noqa: S603
                ["docker", "ps", "-a", "--filter", f"name=^{CONTAINER_NAME}",
                 "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("could not list stray exec containers: %s", e)
            return []
        names = [n.strip() for n in (listed.stdout or "").splitlines() if n.strip()]
        removed = []
        for name in names:
            try:
                done = subprocess.run(  # noqa: S603
                    ["docker", "rm", "-f", name], capture_output=True, text=True,
                    timeout=_PROBE_TIMEOUT_S, check=False)
            except (OSError, subprocess.SubprocessError) as e:
                logger.warning("could not remove %s: %s", name, e)
                continue
            if done.returncode == 0 and done.stdout.strip():
                removed.append(name)
        return removed

    # -- shared argv ------------------------------------------------------

    def _common_run_args(self, spec: ExecSpec) -> list:
        """Mounts, user and env shared by the one-shot and held forms.

        Only reachable from ``docker run``: a later ``docker exec`` can add env but not
        mounts, which is why ``spec.gui`` is part of the held container's identity rather
        than something a follow-up call can turn on.
        """
        args = ["--user", f"{os.getuid()}:{os.getgid()}"]
        if spec.gui:
            # The socket lets the container talk to the host's X server; /dev/dri gives it
            # the render node, so GL is hardware-accelerated where the host has a GPU. The
            # grant is ours to make — there is no run.sh on this path to do it.
            grant_local_access()
            args += ["-v", "/tmp/.X11-unix:/tmp/.X11-unix:rw"]
            if os.path.exists("/dev/dri"):
                args += ["-v", "/dev/dri:/dev/dri"]
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
