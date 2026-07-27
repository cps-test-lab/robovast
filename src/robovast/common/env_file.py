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

"""Loading the project ``.env``.

Read **once per ``vast`` invocation**, by the CLI group callback before any command runs
(:func:`robovast.common.cli.cli.cli`), so every variable in the file is simply part of
the environment — no command needs to know that a value came from a ``.env``, and none
can forget to look. Per-command loaders drifted: a value resolved *before* the load
ignored the file.

Which file: **``./.env``, the current directory only**. A ``.env`` elsewhere is not
consulted — searching the project config's directory and the project dir used to mean
that initialising a project in a parent directory silently took a working ``.env`` out
of scope.
"""

import os

#: Suffix marking a value that names a file on disk (``ROBOVAST_REGISTRY_CA_FILE``,
#: ``ROBOVAST_GCS_KEY_FILE``, ``ROBOVAST_SFTP_KEY_FILE``, …). A convention rather than a
#: list, so a new credential file needs no change here.
FILE_KEY_SUFFIX = "_FILE"

ENV_FILE = ".env"


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
    """Load *path* into ``os.environ``, refusing values that name missing files.

    A ``*_FILE`` entry pointing at nothing is a configuration error worth stopping for:
    the consumer is typically a deployment step that would otherwise proceed *without*
    the credential or CA and fail much later, somewhere unrelated — a push rejected for
    untrusted TLS, say, rather than "the CA file you named is not there".

    ``override`` stays False: a real environment variable beats a ``.env`` line. A
    missing ``.env`` is a no-op.

    Raises:
        ValueError: listing every unusable file reference, with the directory they were
            resolved against (relative values follow the CWD, like the file itself).
    """
    from dotenv import dotenv_values, load_dotenv
    if not os.path.isfile(path):
        return
    problems = _file_problems(dotenv_values(path))
    if problems:
        raise ValueError(
            f"{os.path.abspath(path)} names files that cannot be used "
            f"(relative paths resolve against {os.getcwd()}):\n  - "
            + "\n  - ".join(problems))
    load_dotenv(path, override=False)
