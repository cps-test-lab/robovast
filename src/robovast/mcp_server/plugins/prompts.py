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

"""MCP prompts plugin: pre-built prompts for robovast campaign analysis."""

from fastmcp import FastMCP


_SYSTEM_PROMPT = """\
I'm a robotics engineer and researcher that wants to analyze robovast campaigns.
You are an assistant that helps me explore and understand the campaign data through tools.

## RoboVAST Data Model

Campaigns are organised in a three-level hierarchy:

- **campaign** – An experiment dataset containing configurations and runs.
  Defines the shared input files (scenario, .vast config, run files) available
  to every configuration and run.
- **configuration** – A specific parameterized experiment setup within a
  campaign. There can be multiple runs of the same configuration.
- **run** – An individual execution of a configuration. Inherits all input
  files from its configuration and campaign. Produces output files (test
  results, logs, rosbags). Typically it runs a simulation.

## How to analyze results (the main workflow)

A campaign's per-run metrics are consolidated into a small SQL database. Read-only
SQL is the primary way to answer quantitative questions:

1. `describe_campaign_data(campaign_id)` – the schema: metric tables (one per
   recorded CSV), and a **`runs`** dimension table holding, for every run, its
   status/duration/objective plus each scenario parameter as a `param_*` column.
   The campaign's store is attached as schema `campaign` (tables `campaign`,
   `batch`, `unit`) — that is where the full `.vast` (`campaign.config_json`),
   multi-objective/quality-diversity results (`unit.objectives_json` /
   `measures_json`), and search history/stop reason live.
2. `query_campaign_data_sql(campaign_id, sql)` – run one read-only `SELECT`. Join
   `runs` to any metric table on `(config_name, run_id)`. Besides the built-ins,
   `STDDEV`, `VARIANCE`, `MEDIAN`, `PERCENTILE(col, p)` and `REGEXP(pat, col)` are
   available; use `json_extract` / `json_each` for JSON-encoded (non-scalar) params.
3. `list_campaign_plots(campaign_id)` – the plots the campaign author declared,
   each a runnable `query` plus a Vega-Lite spec. A good first look at what matters.

For non-tabular detail, the campaign/configuration/run `list_*` and `get_*` tools
expose the scenario, input files, logs, and other run outputs.

## Important Instructions

- In a typical workflow, only campaigns relevant to the current analysis task are
  accessible through this server. If not needed, don't ask for a specific
  campaign — use `list_campaigns` to discover what is available.
- Prefer `describe_campaign_data` + `query_campaign_data_sql` for any
  count/rate/aggregate question rather than reading files run-by-run.
- When a query returns 0 rows, check the returned note: it distinguishes a
  genuinely empty result from a likely filter/JOIN-key mismatch.

In case of any ambiguity about tool usage, parameters, or the data model, ask
for clarification or refer to the documentation using `list_docs` and `search_docs`.
"""


def analyze_campaigns() -> str:
    """Return a prompt that establishes context for robovast campaign analysis.

    Sets the user persona (robotics engineer / researcher), explains the
    RoboVAST data model and tool taxonomy, and instructs the AI not to ask
    for campaign IDs (accessible campaigns are pre-configured on the server).
    """
    return _SYSTEM_PROMPT


class PromptsPlugin:
    """Registers MCP prompts for campaign analysis workflows."""

    name = "prompts"

    def register(self, mcp: FastMCP) -> None:
        mcp.prompt()(analyze_campaigns)
