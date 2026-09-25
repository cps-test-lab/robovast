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

"""A RoboVAST campaign's data in pandas and SQL, from its directory, its archive, or several.

    from robovast_data import Campaign
    c = Campaign("~/Downloads/<campaign>")
    c.runs                       # one row per run: outcome, host, param_* per factor
    c.table("poses", run=0, config="<config>")
    c.sql("SELECT config_name, avg(duration_s) FROM runs GROUP BY 1")
"""

from importlib.metadata import PackageNotFoundError, version

from .data import Campaign, ConfigFiles, Corpus, Data, open_data, read_runs, read_table, scope_of
from .engine import Engine, Problem, Scope
from .remote import RemoteCampaign
from .statement import QueryError

try:
    __version__ = version("robovast-data")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0+unknown"

__all__ = ["Campaign", "ConfigFiles", "Corpus", "Data", "Engine", "Problem", "QueryError",
           "RemoteCampaign", "Scope", "__version__", "open_data", "read_runs", "read_table", "scope_of"]
