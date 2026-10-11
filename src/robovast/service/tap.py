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

"""The tap: a command started in a live job's simulation container, relayed line by line.

The relay between a runner's :meth:`~robovast.service.container_exec.ExecRunner.stream_in`,
which hands lines to a callback from the thread that runs the exec, and a reader that wants
an iterator it can leave -- an SSE route ticking beside a disconnect check, a CLI printing
until the bound, an MCP tool collecting for a few seconds. One thread per tap runs the exec;
a bounded queue carries its lines; closing the stream is what ends the exec.

What is *not* here: which command runs, whether the job is running, and the probe record.
Those are :meth:`~robovast.service.service_base.ServiceBase.tap_job`'s, which builds one of
these once it has decided all three.
"""

import queue
import threading
import time
from typing import Callable, Optional

from robovast.service.interface import TapEnd, TapRow

#: Most lines held between the exec thread and the reader. Past it the exec thread waits for
#: the reader rather than dropping lines, so a fast topic reaches a slow reader whole and
#: late instead of with gaps nobody is told about; the tap's time bound caps the wait.
QUEUE_MAX = 10000

#: How long a put on a full queue waits before checking whether the tap was closed meanwhile.
_PUT_SLICE_S = 0.2


class TapStream:
    """The rows of one tap, as an iterator, and the handle that ends it.

    Iterating yields :class:`~robovast.service.interface.TapRow` per line the command prints
    and one :class:`~robovast.service.interface.TapEnd` last; :meth:`poll` is the same read
    with a timeout, for a reader that has something else to check between lines. :meth:`close`
    ends the exec (through the runner's ``should_stop``) and is safe to call twice; the
    ``on_close`` callback runs exactly once, when the exec thread has finished, which is what
    lets a caller keep "one tap per job" true without watching the thread itself.
    """

    def __init__(self, run: Callable[[Callable[[str], None], Callable[[], bool]], tuple],
                 *, on_close: Optional[Callable[[], None]] = None):
        """*run(on_line, should_stop)* performs the exec and returns ``(code, timed_out)``."""
        self._queue: "queue.Queue" = queue.Queue(maxsize=QUEUE_MAX)
        self._stop = threading.Event()
        self._done = threading.Event()
        self._on_close = on_close
        self._end: Optional[TapEnd] = None
        self._exhausted = False
        self._thread = threading.Thread(target=self._run, args=(run,), name="robovast-tap",
                                        daemon=True)
        self._thread.start()

    def _run(self, run) -> None:
        try:
            code, timed_out = run(self._relay, self._stop.is_set)
            self._put(TapEnd(exit_code=code, timed_out=timed_out))
        except Exception as err:  # noqa: BLE001 - carried to the reader, which raises it
            self._put(err)
        finally:
            self._done.set()
            if self._on_close is not None:
                self._on_close()

    def _relay(self, line: str) -> None:
        self._put(TapRow(t_wall=time.time(), line=line))

    def _put(self, item) -> None:
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=_PUT_SLICE_S)
                return
            except queue.Full:
                continue

    def poll(self, timeout_s: float):
        """The next row, :class:`TapEnd`, or ``None`` when nothing arrived within *timeout_s*.

        Raises what the exec raised, once, in the reader's thread -- a runner that could not
        open the exec is the reader's failure to report, not a silent end.
        """
        if self._end is not None:
            return self._end
        try:
            item = self._queue.get(timeout=timeout_s)
        except queue.Empty:
            return None
        if isinstance(item, BaseException):
            self._end = TapEnd(exit_code=None)
            raise item
        if isinstance(item, TapEnd):
            self._end = item
        return item

    def __iter__(self):
        return self

    def __next__(self):
        if self._exhausted:
            raise StopIteration
        while (item := self.poll(1.0)) is None:
            pass
        if isinstance(item, TapEnd):
            self._exhausted = True
        return item

    @property
    def ended(self) -> bool:
        """True once the end has been read: nothing more will come."""
        return self._end is not None

    def close(self) -> None:
        """End the exec, if it is still running, and let the reader go. Idempotent.

        A closed tap reads as ended with no exit code: the process was cut off, not waited
        for, and a reader that closes and then reads gets that answer rather than a wait.
        """
        self._stop.set()
        if self._end is None:
            self._end = TapEnd(exit_code=None)
        # Whatever the exec thread is blocked on putting is not wanted any more.
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def join(self, timeout_s: float) -> bool:
        """Wait for the exec thread to finish; True when it has."""
        return self._done.wait(timeout_s)
