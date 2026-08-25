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

"""Printing a ``LogChunk`` log to a terminal, in one place.

Every live log this service serves is the same shape -- ``fetch(offset) -> LogChunk``,
where ``next_offset`` says where to resume and ``eof`` says there will be no more. The
browser tails them over SSE (``app.py``'s ``_sse_log_stream``); a terminal polls. That poll
had been written once per command, and the copies had already drifted: different cadences,
and one advanced the offset only when a chunk carried text -- which stalls forever on a
server that reports progress with an empty delta, exactly the case ``next_offset`` exists
to express.
"""

import time

#: How often to ask for more while following. Slower than the browser's 0.5s SSE tick on
#: purpose: this is one HTTP round trip per poll, where the stream is one connection.
POLL_S = 1.5


def tail_chunks(fetch, echo, *, follow: bool = True, offset: int = 0,
                poll_s: float = POLL_S) -> int:
    """Print *fetch*'s log from *offset*, and return where it stopped.

    Stops at ``eof`` -- the log is over and nothing more will be written -- or immediately
    after the first read when *follow* is false.

    ``next_offset`` is followed whether or not the chunk carried text, because it is the
    server's statement about the stream and a chunk can legitimately advance without
    printable content. Returning the final offset lets a caller resume; nothing needs that
    yet, and it costs a word to keep the contract honest rather than swallowing it.
    """
    while True:
        chunk = fetch(offset)
        if chunk.text:
            echo(chunk.text)
        offset = chunk.next_offset
        if chunk.eof or not follow:
            return offset
        time.sleep(poll_s)
