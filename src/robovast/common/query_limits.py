"""What one query may use, as the operator states it.

The engine bounds a query with DuckDB's own ``threads`` and ``memory_limit`` settings and
a timer; its defaults are a few threads and DuckDB's own memory ceiling (most of the RAM
it can see). A service sharing a node with running campaigns, or one answering many
queries at once, wants those bounded lower, so both are read from the operator's ``.env``
here and handed to every engine the service builds. Like the disk reserve, an invalid
value is an error naming the variable, not a fallback: a bound that silently fell back to
the default would not be the one its operator meant.
"""

import os
import re
from typing import Dict

#: A DuckDB memory limit such as ``4GB`` or ``512MiB``; unset leaves DuckDB's own ceiling.
MEMORY_ENV = "ROBOVAST_QUERY_MEMORY"
#: The threads one query may use; unset leaves the engine's default.
THREADS_ENV = "ROBOVAST_QUERY_THREADS"

_MEMORY = re.compile(r"^\d+(\.\d+)?\s*([KMGT]i?B|bytes?)$", re.IGNORECASE)


def query_limits() -> Dict[str, object]:
    """The engine options the environment states: ``memory_limit`` and ``threads`` when set.

    Raises, naming the variable, on a value DuckDB would not accept.
    """
    options: Dict[str, object] = {}
    memory = os.environ.get(MEMORY_ENV, "").strip()
    if memory:
        if not _MEMORY.match(memory):
            raise ValueError(f"{MEMORY_ENV} must be a DuckDB memory limit such as 4GB or "
                             f"512MiB; it is {memory!r}")
        options["memory_limit"] = memory
    threads = os.environ.get(THREADS_ENV, "").strip()
    if threads:
        if not threads.isdigit() or int(threads) < 1:
            raise ValueError(f"{THREADS_ENV} must be a positive number of threads; "
                             f"it is {threads!r}")
        options["threads"] = int(threads)
    return options


__all__ = ["MEMORY_ENV", "THREADS_ENV", "query_limits"]
