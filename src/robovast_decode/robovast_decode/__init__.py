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

"""Tables from a campaign's recordings, in plain Python: no ROS, no execution image.

What a reader may rely on -- table names, column names and types, the views -- is the
**data contract**, numbered by :data:`DATA_CONTRACT`. Where and how the tables are stored
(``.cache/``, its manifest, the parquet layout) is private to this package and changes
without notice; the contract number moves only when a name or a type a reader addresses
does, and the changelog names the tables.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("robovast-decode")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0+unknown"

#: The version of what a reader addresses: table names, column names and types, views.
#: 2: a field declared as an array is a list column; a sequence of messages is one list per
#: leaf field; laser scans are tables.
DATA_CONTRACT = 2
