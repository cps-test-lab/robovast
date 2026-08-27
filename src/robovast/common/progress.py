#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
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

"""Re-export of the progress helpers, which now live in robovast-client.

They moved there with ``vast campaign import``: a verb that uploads an archive over HTTP
should not need the core installed to format a byte count. Core depends on the client and
never the reverse, so the import direction here is the allowed one.

Re-exported rather than relocated-and-updated so the callers across core and
robovast-cluster keep working -- the same treatment ``robovast.execution.control_server``
gives ``robovast.client.status``.
"""

from robovast.client.progress import (  # noqa: F401  # pylint: disable=unused-import; Re-exported on purpose: see this module's docstring. flake8 needs the noqa,; pylint needs the disable, and neither implies the other.
    ProgressBar, fmt_size, make_transfer_progress_callback)
