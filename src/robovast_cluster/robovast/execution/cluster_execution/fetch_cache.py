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

"""How long the cluster service keeps a campaign's fetched files that nobody reads.

The fetch cache holds copies of object-store data, one directory per campaign, fetched to serve
results, notebooks and plugin endpoints. Without an expiry it grows with every campaign anybody
has ever looked at. A directory left unread for the maximum age is removed on the service's own
schedule, under the same rules a manual clear follows; the next reader fetches it again.

Its own module so ``vast cluster setup`` can validate the setting without importing the service.
"""

import math
import os

#: Where the maximum age is configured, in days; ``0`` keeps entries until they are cleared.
#: Set in the operator's ``.env``; ``vast cluster setup`` and ``vast service upgrade`` carry it
#: into the service Deployment.
MAX_AGE_ENV = "ROBOVAST_FETCH_CACHE_MAX_AGE_DAYS"

#: What an unset maximum age means. Long enough that a campaign being analysed this week stays
#: warm; short enough that the cache does not become a second copy of the object store.
DEFAULT_MAX_AGE_DAYS = 7.0


def max_age_days() -> float:
    """The configured maximum age in days. Raises, naming the variable, on a value that is not
    a non-negative number -- falling back would keep, or delete, what the operator did not ask
    for."""
    raw = os.environ.get(MAX_AGE_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_DAYS
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{MAX_AGE_ENV} must be a number of days (for example 7), or 0 to "
                         f"keep fetched campaigns until they are cleared; it is {raw!r}")
    return value
