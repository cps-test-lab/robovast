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

from mcp.server.fastmcp import FastMCP


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
- **run** – An individual execution of a configuration.  Inherits all input
  files from its configuration and campaign. Produces output files (test
  results, logs, rosbags). Typically it runs a simulation.
- **run_data** – Structured tabular output derived from a run. It is accessible
  through query/inspect tools like `query_run_data_table`,`inspect_run_data_table`.

## Tool Naming Convention

Tools follow the pattern `<verb>_<resource>[_<detail>]`.  Available verbs:

- **get** – Retrieve a specific structured metadata object.
- **list** – Enumerate objects within a resource.
- **query** – Retrieve filtered record sets with pagination.
- **search** – Filter and query resources by criteria.
- **inspect** – Compute derived analysis or statistics.
- **draw** – Render a visual image from data (map overlays, plots).
- **display** – Show a captured image (e.g. simulation screenshot).

## Important Instructions

- **In a typical workflow, only campaigns relevant to the current
  analysis task are accessible through this server. So if not needed, don't ask
  for a specific campaign. Use `list_campaigns` to discover what is available.
- If not requested otherwise, start with campaign-level summaries before 
  drilling into individual configurations or runs.

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
