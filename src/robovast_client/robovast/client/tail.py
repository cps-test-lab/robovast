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

"""Printing a log to a terminal, in one place.

Two shapes of log reach a terminal. The byte-addressed ones -- the service's own log, an
image build's -- are ``fetch(offset) -> LogChunk``, where ``next_offset`` says where to
resume and ``eof`` says there will be no more; a terminal polls them
(:func:`tail_chunks`). The campaign log is rows after a cursor
(:class:`~robovast.service.interface.CampaignLogChunk`), pulled once or followed over the
service's stream; :func:`tail_rows` prints whichever it is given, rendering every row as
:func:`format_campaign_log_row` does -- the one rendering, which the MCP tool reads
through as well.
"""

import time
from typing import Callable, Iterable

from robovast.service.interface import CampaignLogChunk, CampaignLogRow

#: How often to ask for more while following a byte-addressed log. Slower than the
#: browser's 0.5s SSE tick on purpose: this is one HTTP round trip per poll, where the
#: stream is one connection.
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


def format_campaign_log_row(row: CampaignLogRow) -> str:
    """*row* as ``[PHASE] <time> <LEVEL> <logger>: <message>``.

    The time is the row's stamp in this process's local time; a ``NOTE`` row has none and
    no logger, so it reads ``[PHASE] NOTE: <message>``. A continuation line of the message
    is indented under the first, so a traceback stays one row to the eye.
    """
    stamp = ""
    if row.wall_ts is not None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row.wall_ts)) + " "
    who = f"{row.level} {row.logger}: " if row.logger else f"{row.level}: "
    first, *rest = row.message.split("\n")
    return "\n".join([f"[{row.phase}] {stamp}{who}{first}"] + [f"    {line}" for line in rest])


def tail_rows(chunks: Iterable[CampaignLogChunk], echo: Callable[[str], None], *,
              as_json: bool = False) -> str:
    """Print every row of *chunks*, and return the cursor the last chunk reached.

    *chunks* is one pulled chunk, or the stream a follow reads; printing stops at the
    first chunk that says ``eof``, which a follow's stream ends on. With *as_json* each
    row is one JSON object on its own line, the interface's field names.
    """
    cursor = ""
    for chunk in chunks:
        for row in chunk.rows:
            echo(row.model_dump_json() if as_json else format_campaign_log_row(row))
        cursor = chunk.cursor or cursor
        if chunk.eof:
            break
    return cursor
