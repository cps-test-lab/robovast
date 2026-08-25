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

"""When a wait should stop believing in the service it is polling.

Both waits beside this module -- :mod:`campaign_wait` and :mod:`image_build_wait` -- hold
that *a failed poll is not a failed wait*, and they are right: restarting the service
mid-run drops one status read while the work continues, and ending the wait over that would
report a live campaign as lost for the price of a hiccup.

Taken without a bound, though, the same rule says a wait against a service that is simply
gone should continue forever, printing nothing, since every poll fails the same silent way.
That is not the property anyone wanted; it is the property nobody looked at. So the rule
becomes: *a failed poll is not a failed wait, until polls stop succeeding altogether.*

The limit is expressed in **seconds of continuous failure**, not in a count of attempts, so
it means the same thing whether a caller polls every two seconds or every thirty. One
success resets it. It lives in its own module because both waits must apply it identically
-- fixing one of a matched pair is how the two loops this codebase deliberately unified
would have drifted apart again.
"""

import time

#: How long every poll may fail before the wait gives up. Generously longer than a service
#: restart or a rolling upgrade (which is the event the tolerance exists for) and far
#: shorter than the indefinite silence it replaces.
STALE_POLL_LIMIT_S = 300.0


class PollsStopped(RuntimeError):
    """Every poll failed for :data:`STALE_POLL_LIMIT_S`; the wait gave up, the work did not.

    Deliberately not ``TimeoutError``: that one means the *work* took too long and the
    caller stopped waiting, and it leaves a bounded wait safe to retry. This one means the
    *service* stopped answering, which is a different thing to go and look at -- so a
    caller that distinguishes them can say which, instead of blaming a campaign for its
    service being down.
    """

    include_traceback = False


class StalePolls:
    """Tracks how long a wait's polls have been failing, and when to stop.

    Usage is one call per poll, either ``succeeded()`` or ``failed(exc)``, then
    ``check(what)``:

        polls = StalePolls()
        while True:
            try:
                status = client.get_status(cid)
            except Exception as e:
                polls.failed(e)
            else:
                polls.succeeded()
            ...
            polls.check(f"campaign {cid!r}")
    """

    def __init__(self, limit_s: float = STALE_POLL_LIMIT_S):
        self._limit_s = limit_s
        self._failing_since = None
        self._last_error = None

    def succeeded(self) -> None:
        """One poll succeeded: the service is answering, so nothing is stale."""
        self._failing_since = None
        self._last_error = None

    def failed(self, error: BaseException) -> None:
        """One poll failed. The *first* failure starts the clock; later ones only update
        what to report, so the window measures continuous failure rather than the last
        attempt."""
        if self._failing_since is None:
            self._failing_since = time.monotonic()
        self._last_error = error

    def check(self, what: str) -> None:
        """Raise :class:`PollsStopped` if nothing has answered for the whole window.

        *what* names the thing being waited on, for the message.
        """
        if self._failing_since is None:
            return
        elapsed = time.monotonic() - self._failing_since
        if elapsed < self._limit_s:
            return
        # Careful with what this claims. The obvious wording -- "the work is still running,
        # only the service is unreachable" -- is an assertion this code cannot make: an id
        # nothing ever started fails every poll in exactly the same way, and there is no
        # work behind it to still be running. So it says what is known (nothing could be
        # read) and names both candidates, rather than blaming the service for a typo.
        raise PollsStopped(
            f"no status could be read for {what} in {elapsed:.0f}s -- every poll failed, "
            f"the last with: {self._last_error}. Nothing here says the work failed: it was "
            f"never asked successfully. Check that the robovast service is up and "
            f"reachable, and that the id exists -- one that does not fails every poll the "
            f"same way. Whatever is running is untouched and can be waited on again.")


__all__ = ["STALE_POLL_LIMIT_S", "PollsStopped", "StalePolls"]
