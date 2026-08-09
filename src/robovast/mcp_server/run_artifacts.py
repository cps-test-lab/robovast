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

"""Reaching a run's *files* from an MCP tool, for the readers that need a real path.

Most tools read a campaign through SQL (:mod:`robovast.mcp_server.data_access`) or hand back
an address for the caller to fetch. A few have to run a program over a file — ffmpeg decoding
one frame of a recording — and a program takes a path, not an address. On a cluster campaign
there is no path: the file is an object. Fetching the bytes and writing them into a temp dir
is what lets such a reader work on either lane instead of only the local one.

Deliberately small, and deliberately not a general download helper: every byte fetched here is
a byte held in the service's memory on the way through, so this is for files a tool must
*execute against*, not for files a caller merely wants.
"""

import contextlib
import logging
import tempfile
from pathlib import Path

from robovast.common.file_address import RESULTS, format_address
from robovast.mcp_server import service_access

logger = logging.getLogger(__name__)


class RunArtifactError(Exception):
    """A run file could not be reached, with the address in the message.

    Raised rather than returned: the tools here hand back an image, which has no result dict
    to carry an ``{"error": ...}`` in.
    """


def run_address(campaign_id: str, config_name, run_id, *parts) -> str:
    """The ``/results/<campaign>/<config>/<run>/<path>`` address for a run's file."""
    return format_address(RESULTS, campaign_id, "/".join(
        str(p).strip("/") for p in (config_name, run_id, *parts) if str(p)))


@contextlib.contextmanager
def materialized(address: str, filename: str):
    """Fetch one addressed file into a temp dir and yield its path; delete it after.

    *filename* is the name it takes on disk — kept meaningful (rather than a random temp
    name) because a decoder's error messages quote it, and "no such stream in run.npz" is a
    better line than one naming ``tmpxa4f1``.
    """
    client = service_access.service_client()
    if client is None:
        raise RunArtifactError(service_access.NO_SERVICE)
    try:
        data = client.read_file_bytes(address)
    except Exception as e:  # noqa: BLE001 - the address is the useful half of any of these
        raise RunArtifactError(f"could not read {address}: {e}") from e
    with tempfile.TemporaryDirectory(prefix="robovast-artifact-") as tmp:
        path = Path(tmp) / filename
        path.write_bytes(data)
        yield path
