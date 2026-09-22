# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast service mcp-stats`` -- the terminal view of the MCP call record.

The property under test is the one the record was silently breaking: an answer that is
part of the log must not be presentable as all of it.
"""

from robovast.client.service_cli import _all_mcp_calls
from robovast.service.interface import McpCall, McpCalls


def _call(tool):
    return McpCall(at=1.0, tool=tool, duration_ms=1.0, ok=True)


class _Paged:
    """A service that answers at most ``page`` rows at a time, like the real route."""

    def __init__(self, total, page):
        self._rows = [_call(f"t{i}") for i in range(total)]
        self._page = page
        self.requests = []

    def mcp_calls(self, limit=200, tool="", failed_only=False, offset=0):
        self.requests.append((limit, offset))
        window = self._rows[offset:offset + min(limit, self._page)]
        return McpCalls(calls=window, total=len(self._rows), limit=limit, offset=offset,
                        truncated=offset + len(window) < len(self._rows))


def test_a_large_window_follows_the_pages_instead_of_stopping_at_one():
    """Asking for the whole record used to return one page that looked like the record.

    The route bounds one response; the record holds far more. A client that asked for more
    than a page and reported what came back was reporting a window many times narrower than
    the one it asked for, with nothing saying so.
    """
    service = _Paged(total=25, page=10)

    answer = _all_mcp_calls(service, limit=25, tool="", failed=False)

    assert [c.tool for c in answer.calls] == [f"t{i}" for i in range(25)]
    assert answer.total == 25
    assert answer.truncated is False
    assert len(service.requests) == 3, "one request per page, and no more"


def test_a_window_smaller_than_the_record_is_reported_as_partial():
    service = _Paged(total=25, page=10)

    answer = _all_mcp_calls(service, limit=15, tool="", failed=False)

    assert len(answer.calls) == 15
    assert answer.total == 25
    assert answer.truncated is True, "fewer rows than matched is a partial answer"


def test_a_record_pruned_mid_walk_reports_what_was_read():
    """Retention deletes rows while the walk is in progress; that must terminate.

    The log prunes as it is written, so a page can come back empty while the total still
    claims more. Looping until the total is reached would never finish.
    """

    class _Shrinking(_Paged):
        def mcp_calls(self, limit=200, tool="", failed_only=False, offset=0):
            answer = super().mcp_calls(limit, tool, failed_only, offset)
            if offset:
                answer.calls = []  # everything past the first page aged out
            return answer

    answer = _all_mcp_calls(_Shrinking(total=25, page=10), limit=25, tool="", failed=False)

    assert len(answer.calls) == 10
    assert answer.truncated is True, "a short read is not a complete one"
