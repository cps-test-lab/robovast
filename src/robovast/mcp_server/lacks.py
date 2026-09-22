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

"""Arguments a tool lacks on purpose, and why.

A caller that reaches for an argument a tool lacks is rejected before the tool runs, so the
tool's reply -- which may explain the same thing -- never arrives. The reason belongs in the
rejection: it costs nothing until a caller needs it, where a sentence in the description is
sent with every request. :func:`~robovast.mcp_server.server._argument_help` adds it to the
error; a tool only declares it::

    @lacks(timeout="a command gets a fixed cap, a scenario its execution.timeout")
    def exec_in_container(...): ...

    @lacks("there is one at a time, so there is nothing to name")
    def stop_container(): ...

Stored on the function, and read from the registered tool, so a declaration goes wherever
the function is registered and nowhere else.
"""

from typing import Callable

_LACKS_ATTR = "__robovast_arguments_it_lacks__"
_LACKS_ALL_ATTR = "__robovast_lacks_all__"


def lacks(lacks_all: str = "", /, **reasons: str) -> Callable:
    """Declare the arguments a tool lacks on purpose, each with the reason a caller is told.

    *lacks_all* is the reason a tool takes no arguments at all, said to a caller who passes
    any. Each keyword names an argument the tool lacks and what to do instead.
    """
    def mark(fn: Callable) -> Callable:
        setattr(fn, _LACKS_ALL_ATTR, lacks_all)
        setattr(fn, _LACKS_ATTR, dict(reasons))
        return fn
    return mark


def arguments_it_lacks(tool) -> dict:
    """``{argument: reason}`` a registered *tool* declared; empty when it declared none."""
    return dict(getattr(getattr(tool, "fn", None), _LACKS_ATTR, None) or {})


def why_it_takes_none(tool) -> str:
    """Why a registered *tool* takes no arguments, or ``""`` when it has not said."""
    return getattr(getattr(tool, "fn", None), _LACKS_ALL_ATTR, "") or ""
