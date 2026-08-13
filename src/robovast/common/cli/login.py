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
from pathlib import Path

#: Overridable so a test never touches a developer's real login, and so a CI job can
#: point at its own.
CONFIG_ENV_VAR = "ROBOVAST_CONFIG"


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
