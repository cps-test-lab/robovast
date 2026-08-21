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

"""Where ``vast login`` keeps the service URL, the shared token, and your name.

Per **user**, not per project: ``~/.config/robovast/config.json`` rather than the
project ``.env``. Which instance you talk to and who you say you are follow the person,
while a ``.env`` follows a checkout — and a token in a project directory is a token one
``git add -A`` away from being published.

The stored shape is deliberately wider than today's needs::

    {"url": ..., "name": ..., "auth": {"type": "shared-secret", "token": ...}}

``auth`` is an object rather than a bare token string so that ``type`` can become
``"oidc"`` — with ``expires_at`` and a refresh token beside it — without changing the
file's shape or ``vast login``'s interface. That is the whole reason for the nesting:
one shared secret needs no structure, but the upgrade to real identity should not have
to migrate everybody's config.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

#: Overridable so a test never touches a developer's real login, and so a CI job can
#: point at its own.
CONFIG_ENV_VAR = "ROBOVAST_CONFIG"

#: A scheme, per RFC 3986: a letter followed by letters/digits/``+-.``, then ``://``.
#: Matched rather than handed to ``urlsplit`` because ``urlsplit`` reads the host of a
#: bare ``host:port`` as a *scheme* -- so ``localhost:8800`` would come back with
#: ``scheme="localhost"`` and no host at all.
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def normalize_url(url: str) -> str:
    """The service URL as the client should use it, from what a person typed.

    A bare host is what an operator actually hands out -- they say "the service is at
    robovast.example.org", not "at https://robovast.example.org" -- so a missing scheme
    is filled in rather than failing on a URL whose meaning is unambiguous. Deployed
    services are behind TLS, hence ``https``; loopback is the one place where they are
    not, so ``localhost:8800`` becomes ``http`` and not an unexplainable TLS error
    against a plain ``vast serve``.

    A scheme that *is* given is kept, and anything other than http/https is refused
    here: the client speaks HTTP, and a wrong scheme should be a sentence now rather
    than a connection error from the next unrelated command.
    """
    url = url.strip().rstrip("/")
    if not url:
        return ""
    if not _SCHEME.match(url):
        host = url.split("/", 1)[0].split(":", 1)[0].lower()
        local = host in ("localhost", "127.0.0.1", "::1", "[::1]") or host.endswith(".localhost")
        url = f"{'http' if local else 'https'}://{url}"
    scheme = url.split("://", 1)[0].lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"{scheme}:// is not a service URL — robovast speaks http(s)")
    return url


def config_path() -> Path:
    """The login file, honouring ``ROBOVAST_CONFIG`` and then ``XDG_CONFIG_HOME``."""
    override = os.environ.get(CONFIG_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "robovast" / "config.json"


def load() -> dict:
    """The stored login, or ``{}``.

    A corrupt file reads as "not logged in" rather than crashing every command: the
    remedy is the same (``vast login`` again), and an unreadable config must not make
    ``vast --help`` fail.
    """
    path = config_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(url: str, token: str, name: str = "") -> Path:
    """Write the login, readable only by this user."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "url": url.rstrip("/"),
        "name": name,
        "auth": {"type": "shared-secret", "token": token},
    }
    # Create with 0600 from the start rather than chmod-ing afterwards: between the two
    # there is a moment when a secret is world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return path


def clear() -> bool:
    """Remove the stored login. True if there was one."""
    try:
        config_path().unlink()
        return True
    except FileNotFoundError:
        return False


def credentials() -> tuple[str, str, str]:
    """``(url, token, name)`` from the stored login; empty strings when absent."""
    data = load()
    auth = data.get("auth") or {}
    if not isinstance(auth, dict):
        auth = {}
    return (str(data.get("url") or ""),
            str(auth.get("token") or ""),
            str(data.get("name") or ""))


def mcp_add_command(url: str, token: str, name: str = "") -> list[str]:
    """The ``claude mcp add`` invocation registering this service with an agent, as lines.

    Rendered here because it is printed from two places — after ``vast login`` and by
    ``vast exec cluster token`` — and a header that drifts out of one copy is a whole
    class of confusing failure: a missing ``Authorization`` 401s loudly, but a missing
    name silently records every campaign that agent starts as unattributed.

    Returned as one line per argument so each caller can indent the continuations to fit
    its own output; join with ``" \\\\\\n<indent>"``. Every argument is shell-quoted, so a
    name with a space or a quote in it stays one argument.
    """
    import shlex

    from robovast.service.auth import USER_HEADER

    lines = [f"claude mcp add --transport http robovast {url.rstrip('/')}/mcp",
             "--header " + shlex.quote(f"Authorization: Bearer {token}")]
    if name:
        # ``x-robovast-user`` on the wire; title-cased here because that is how a header
        # is written by hand, and HTTP header names are case-insensitive anyway.
        lines.append("--header " + shlex.quote(f"{USER_HEADER.title()}: {name}"))
    return lines


def default_name() -> str:
    """A sensible name to offer at the prompt, so the common case is one keypress.

    ``git config user.name`` first — this is a research tool used from checkouts, and it
    is the name the person already publishes their work under — then ``$USER``.
    """
    import subprocess
    try:
        result = subprocess.run(["git", "config", "user.name"],  # noqa: S607
                                capture_output=True, text=True, timeout=5, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return os.environ.get("USER", "")


def _path_dirs() -> list[Path]:
    """Directories on a **fresh login shell's** PATH, not this process's.

    They differ, and the difference is the whole problem: a venv that was activated to
    run ``vast login`` puts its ``bin`` on *this* PATH and on no other, so linking into
    it would look like success and change nothing for the next shell.
    """
    import subprocess  # pylint: disable=import-outside-toplevel
    try:
        out = subprocess.run(["bash", "-lc", "printf %s \"$PATH\""],  # noqa: S603,S607
                             capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return [Path(p) for p in out.stdout.split(os.pathsep) if p]


def cli_path() -> Path:
    """Where the ``vast`` console script this process is running lives."""
    import shutil  # pylint: disable=import-outside-toplevel
    import sys  # pylint: disable=import-outside-toplevel
    argv0 = Path(sys.argv[0])
    if argv0.name == "vast" and argv0.exists():
        return argv0.resolve()
    beside = Path(sys.executable).parent / "vast"
    if beside.exists():
        return beside.resolve()
    found = shutil.which("vast")
    return Path(found).resolve() if found else beside


def link_cli() -> tuple[bool, str]:
    """Make ``vast`` resolvable from any shell; return ``(linked, message)``.

    A venv's console script has an absolute interpreter in its shebang, so a symlink to
    it runs with nothing activated, from any directory. The only thing a later shell has
    to supply is the link's directory on PATH -- which is why the target is chosen from
    a login shell's PATH rather than assumed.

    Never reports success it cannot demonstrate: if the command still does not resolve
    in a fresh login shell afterwards, that is a failure with the export line to fix it,
    not a cheerful message and a command that is still missing when an agent needs it.
    """
    import subprocess  # pylint: disable=import-outside-toplevel
    source = cli_path()
    if not source.exists():
        return False, (f"could not find the 'vast' command to link (looked at {source}). "
                       "Install robovast, or add its venv's bin/ to PATH yourself.")

    dirs = _path_dirs()
    home = Path.home()
    preferred = home / ".local" / "bin"
    candidates = ([preferred] if preferred in dirs else []) + [
        d for d in dirs if d != preferred and home in d.parents]
    for target_dir in candidates:
        link = target_dir / "vast"
        try:
            if link.resolve() == source:
                return True, f"'vast' already resolves via {link}"
            target_dir.mkdir(parents=True, exist_ok=True)
            tmp = link.with_name(f".vast.{os.getpid()}")
            tmp.symlink_to(source)
            os.replace(tmp, link)
        except OSError:
            continue
        check = subprocess.run(["bash", "-lc", "command -v vast"],  # noqa: S603,S607
                               capture_output=True, text=True, timeout=15, check=False)
        if check.returncode == 0:
            return True, f"linked 'vast' into {target_dir} (resolves in a new shell)"
        return False, (
            f"linked {link}, but 'vast' still does not resolve in a login shell. Add "
            f"this to your shell profile and start a new one:\n"
            f"    export PATH=\"{target_dir}:$PATH\"")

    return False, (
        "no directory on your login shell's PATH is writable, so 'vast' cannot be made "
        "available outside this venv. Add its bin/ to your profile and start a new "
        f"shell:\n    export PATH=\"{source.parent}:$PATH\"")
