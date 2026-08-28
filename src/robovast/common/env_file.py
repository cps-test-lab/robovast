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

"""Loading configuration from ``.env`` files.

Read **once per ``vast`` invocation**, by the CLI group callback before any command runs
(:func:`robovast.client.cli.cli`), so every variable in the file is simply part of
the environment — no command needs to know that a value came from a ``.env``, and none
can forget to look. Per-command loaders drifted: a value resolved *before* the load
ignored the file.

Two files, in this order of precedence:

1. a real environment variable — always wins;
2. **``./.env``, the current directory only** — the *project's* settings. A ``.env``
   elsewhere is not consulted: searching the project config's directory as well means
   initialising a project in a parent directory silently takes a working ``.env`` out
   of scope;
3. :func:`user_env_file` — the *user's* settings, for what is true of this machine
   rather than of one project: which registry their images come from, their share
   credentials. Without it every setting has to be repeated per project directory and
   is silently lost by running ``vast`` one level up — the CWD rule above is right for
   a project's own config and wrong for an operator's.

Both are dotenv format, and the order is achieved with ``override=False``: whatever is
loaded first stays, so the project file shadows the user file key by key rather than
wholesale.
"""

import os
from pathlib import Path

#: Suffix marking a value that names a file on disk (``ROBOVAST_REGISTRY_CA_FILE``,
#: ``ROBOVAST_GCS_KEY_FILE``, ``ROBOVAST_SFTP_KEY_FILE``, …). A convention rather than a
#: list, so a new credential file needs no change here.
FILE_KEY_SUFFIX = "_FILE"

ENV_FILE = ".env"

#: Overridable so a test never reads a developer's real settings, and so a CI job can
#: point at its own. Named for the file rather than the directory because that is what a
#: caller wants to redirect; ``ROBOVAST_CONFIG`` (the login file) is deliberately
#: separate — one is credentials this rewrites, the other is settings it only reads.
USER_ENV_FILE_VAR = "ROBOVAST_ENV_FILE"


def user_env_file() -> Path:
    """The user-level settings file, honouring :data:`USER_ENV_FILE_VAR` then XDG.

    ``~/.config/robovast/env``. The same directory ``vast login`` keeps ``config.json``
    in (see :func:`robovast.client.login.config_path`), resolved by the same rules, so
    there is one place a user's RoboVAST configuration lives rather than a second one
    invented for this. ``~/.robovast/`` is *data* — workspaces and caches — and settings
    do not belong there.
    """
    override = os.environ.get(USER_ENV_FILE_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "robovast" / "env"


def _file_problems(values: dict) -> list:
    """Values naming a file that cannot be read, as human-readable problems."""
    problems = []
    for key, value in sorted(values.items()):
        if not key.endswith(FILE_KEY_SUFFIX) or not value:
            continue
        # Checked exactly as the consumer will open it — no ~ expansion, no resolving
        # against anything but the CWD — so a value that passes here cannot fail later.
        if not os.path.isfile(value):
            problems.append(f"{key}={value!r} does not exist")
        elif not os.access(value, os.R_OK):
            problems.append(f"{key}={value!r} is not readable")
    return problems


def load_env_file(path: str = ENV_FILE) -> None:
    """Load the project ``.env`` then the user's, refusing values that name missing files.

    A ``*_FILE`` entry pointing at nothing is a configuration error worth stopping for:
    the consumer is typically a deployment step that would otherwise proceed *without*
    the credential or CA and fail much later, somewhere unrelated — a push rejected for
    untrusted TLS, say, rather than "the CA file you named is not there".

    ``override`` stays False throughout, which is what orders the three sources (see the
    module docstring): a real environment variable beats the project file, and the
    project file beats the user file, key by key. Either file missing is a no-op.

    Raises:
        ValueError: listing every unusable file reference, with the directory they were
            resolved against (relative values follow the CWD, like the file itself).
    """
    _load_one(path)
    _load_one(str(user_env_file()))


def _load_one(path: str) -> None:
    """Load a single dotenv file, or do nothing if it is not there."""
    from dotenv import dotenv_values, load_dotenv
    if not os.path.isfile(path):
        return
    problems = _file_problems(dotenv_values(path))
    if problems:
        raise ValueError(
            f"{os.path.abspath(path)} names files that cannot be used "
            f"(relative paths resolve against {os.getcwd()}):\n  - "
            + "\n  - ".join(problems)
            + "\n\nA relative path in a user-level env file is rarely what you want: it "
              "follows the directory `vast` runs in, not the file's own.")
    load_dotenv(path, override=False)
