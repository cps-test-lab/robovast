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

"""Where local campaigns live — resolved once, for every reader.

The service and the MCP server both need this, and they must agree: a ``vast serve`` that
wrote a campaign somewhere the results reader does not look produces "no such campaign" for
a campaign that plainly exists. Two copies of the precedence rule, each with a comment
saying it has to match the other, is the arrangement that lets them stop matching.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def local_results_root(workspaces_root: Path | None = None) -> Path:
    """The directory local campaigns are written to and read from.

    A service-owned ``results`` directory beside the workspaces store: one stable
    location, the same for every campaign this service runs.

    No branch here reads an initialized CWD project's ``results_dir``: a file
    discovered by walking up to the filesystem root, deciding where a service writes, is a
    surprise rather than a convenience. A caller who wants to choose says so:
    ``vast serve --results-dir DIR``, which the transport honours ahead of this.

    Pure path resolution: the directory need not exist, and asking never creates it, so a
    caller whose campaigns live in an object store (the cluster lane) does not leave a
    stray local directory behind.

    Args:
        workspaces_root: The workspaces store root, when the caller already knows it.
            Omitted, the default location is used.
    """
    if workspaces_root is None:
        from robovast.service.workspaces import default_workspaces_root
        workspaces_root = default_workspaces_root()
    return Path(workspaces_root).parent / "results"
