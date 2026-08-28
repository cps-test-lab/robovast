"""Whether this process can put a window on a screen, and the access to do it.

``show_gui`` is only meaningful when the process that runs ``docker`` sits at a display:
the container renders through a bind-mounted X socket, so the window appears wherever the
*serve host* is — not where the client is. Two of the three deployments can never satisfy
that (an in-cluster service has no X socket; a ``vast serve`` on a headless VM reached
over an SSH tunnel has none either), and neither does a desktop service started outside a
login session.

So the request is refused up front rather than accepted and quietly rendered nowhere. The
refusal names the serve host, because for a tunnelled service the caller is looking at a
perfectly good display of their own and would otherwise read it as a bug.
"""

import glob
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

#: Where an X server puts its unix sockets. A socket here is the second of the two ways a
#: display can be reachable: ``DISPLAY`` may be unset in a daemon's environment even
#: though the machine has a running X server, and the compose/exec wiring defaults to
#: ``:0`` in exactly that case.
_X11_SOCKET_GLOB = "/tmp/.X11-unix/X*"


def host_display() -> str:
    """The display this process would render on, or ``""`` when there is none.

    ``DISPLAY`` wins when set; otherwise a socket implies ``:<n>``, matching the
    ``${DISPLAY:-:0}`` default the generated compose and the exec lane use.
    """
    from_env = (os.environ.get("DISPLAY") or "").strip()
    if from_env:
        return from_env
    sockets = sorted(glob.glob(_X11_SOCKET_GLOB))
    if sockets:
        return f":{os.path.basename(sockets[0])[1:]}"
    return ""


def require_host_display(*, what: str) -> str:
    """Return the display to use, or raise naming *what* was asked for and where.

    Called at request admission — before an image build, a container or a campaign
    directory exists — so a refusal costs nothing and cannot leave a half-started run
    behind.
    """
    display = host_display()
    if not display:
        raise ValueError(
            f"{what} needs an X display on the host running this service, and there is "
            f"none: DISPLAY is unset and no X socket exists at {_X11_SOCKET_GLOB}. The "
            "window would open on the serve host, not on the machine you are calling "
            "from — so a display on your own machine does not help if this service runs "
            "elsewhere (a cluster deployment or a remote `vast serve` can never do "
            "this). For a desktop `vast serve`, start it from a session where DISPLAY is "
            "set, or drop the request to run headless.")
    return display


def gui_by_default(no_gui: bool, *, notify) -> bool:
    """Whether a command whose GUI is a **default** should run windowed.

    The counterpart to :func:`require_host_display`, and the asymmetry is the point: an
    explicit request (``show_gui=True``) is *refused* when it cannot be honoured, while a
    default is *downgraded* — otherwise a local campaign would stop working
    unattended on a build machine because of a flag nobody set.

    Never silently, though: *notify* is called with the reason. The decision also selects
    ``execution.local.gui.parameter_overrides``, so a downgrade that said nothing would
    leave "I expected a window" with no explanation.
    """
    if no_gui:
        return False
    if host_display():
        return True
    notify(f"No X display on this host (DISPLAY unset and no socket at "
           f"{_X11_SOCKET_GLOB}) — running headless.")
    return False


def grant_local_access() -> None:
    """Let containers on this host talk to the X server (``xhost +local:``).

    Non-fatal: access may already be granted, or the host may have no ``xhost`` while
    still accepting connections. But *reported* — a silently failed grant presents as a
    run that started fine and drew nothing.
    """
    try:
        done = subprocess.run(["xhost", "+local:"],  # noqa: S603, S607
                              capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("could not run xhost to grant local X11 access (%s); the "
                       "container may be refused by the X server", e)
        return
    if done.returncode != 0:
        logger.warning("xhost +local: failed (%s); the container may be refused by the "
                       "X server", (done.stderr or done.stdout or "").strip())
