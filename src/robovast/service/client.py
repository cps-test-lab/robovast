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

"""``RobovastClient`` — one interface, two transports (compatibility re-export).

The client is how the ``vast`` CLI, the MCP server, and (later) a web UI reach
RoboVAST operations without caring where they run. The two transports now live in
their own modules — :mod:`robovast.service.local_transport` (in-process) and
:mod:`robovast.service.http_client` (to a running ``robovast-service``) — and are
re-exported here so ``from robovast.service.client import ...`` keeps working.

Both implement :class:`~robovast.service.interface.RobovastInterface`, so a caller
holding a ``RobovastClient`` is transport-agnostic.
"""

from robovast.service.http_client import HTTPTransport, RobovastClient
from robovast.service.local_transport import (  # noqa: F401  (re-exported)
    LocalTransport, _LocalCampaign, _robovast_version)

__all__ = ["LocalTransport", "HTTPTransport", "RobovastClient"]
