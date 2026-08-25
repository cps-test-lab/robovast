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
RoboVAST operations without caring where they run. The two transports live in their own
modules — :mod:`robovast.service.local_transport` (in-process) and
:mod:`robovast.service.http_client` (to a running ``robovast-service``) — and are
re-exported here so ``from robovast.service.client import ...`` keeps working.

Both implement :class:`~robovast.service.interface.RobovastInterface`, so a caller
holding a ``RobovastClient`` is transport-agnostic.

**The in-process transport is re-exported lazily.** Most callers here want
``RobovastClient`` to reach a *running* service — ``campaign_wait``, ``service_target``,
the MCP's service access — and importing this module used to hand them 3,000 lines of
in-process server as well. Under PEP 562 they pay for it only if they name it.
"""

from typing import TYPE_CHECKING

from robovast.service.http_client import HTTPTransport, RobovastClient

_LAZY = {"LocalTransport": "robovast.service.local_transport",
         "_LocalCampaign": "robovast.service.local_transport"}

__all__ = ["LocalTransport", "HTTPTransport", "RobovastClient"]


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


if TYPE_CHECKING:  # for type checkers and IDEs only
    from robovast.service.local_transport import LocalTransport, _LocalCampaign  # noqa: F401
