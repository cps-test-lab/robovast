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

"""``RobovastClient`` — the client's view of a running service (compatibility re-export).

The client is how the ``vast`` CLI, the MCP server and the web UI reach RoboVAST
operations without caring where they run. The transport lives in
:mod:`robovast.service.http_client` and is re-exported here so
``from robovast.service.client import ...`` keeps working; the service's own bookkeeping
class is re-exported lazily so that importing this module costs no more than the client.
"""

from robovast.service.http_client import HTTPTransport, RobovastClient

_LAZY = {"_TrackedCampaign": "robovast.service.service_base"}

__all__ = ["HTTPTransport", "RobovastClient"]


def __getattr__(name):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module  # pylint: disable=import-outside-toplevel
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
