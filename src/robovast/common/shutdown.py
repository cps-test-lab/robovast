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

"""Process-wide "this process is stopping" flag, raised the moment Ctrl+C lands.

Winding down is a race: uvicorn gives the app a few seconds
(``timeout_graceful_shutdown``) and then cancels whatever is still running. Blocking
network I/O that outlives that window is not merely slow — the *resilience* built
into it keeps repairing a connection the process is simultaneously tearing down. The
driver's S3 client is the concrete case: on a timeout it restarts the shared
``kubectl port-forward``, so a read still in flight at Ctrl+C re-opens the tunnel
seconds after the service closed it, and the ``kubectl`` child then outlives the
service that spawned it.

The signal handler flips this flag before uvicorn starts its clock, and the retrying
layers consult it to fail fast rather than rebuild what is being torn down. It is a
plain module-level event rather than state threaded through the service objects
because the code that must see it runs several layers down, on worker threads that
were handed a callable and nothing else.
"""

import threading

_shutting_down = threading.Event()


def begin_shutdown() -> None:
    """Announce that the process is winding down. Idempotent."""
    _shutting_down.set()


def is_shutting_down() -> bool:
    """Whether :func:`begin_shutdown` has been called in this process."""
    return _shutting_down.is_set()


def reset_shutdown() -> None:
    """Clear the flag. For tests — a real process never un-shuts-down."""
    _shutting_down.clear()
