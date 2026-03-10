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

## RoboVAST Data Model

Campaigns are organised in a three-level hierarchy:

- **campaign** – An experiment dataset containing configurations and runs.
  Defines the shared input files (scenario, .vast config, run files) available
  to every configuration and run.
- **configuration** – A specific parameterised experiment setup within a
  campaign.  May add configuration-specific files generated during variation
  (stored in _transient).
- **run** – An individual execution of a configuration.  Inherits all input
  files from its configuration and campaign.  Produces output files (test
  results, logs, rosbags).
- **run_data** – Structured tabular output derived from run artefacts,
  primarily CSV files, exposed through query/inspect tools.
- **artifact** – Files generated or consumed during execution.

## Tool Naming Convention

Tools follow the pattern `<verb>_<resource>[_<detail>]`.  Available verbs:

- **get** – Retrieve a specific structured metadata object.
- **list** – Enumerate objects within a resource.
- **query** – Retrieve filtered record sets with pagination.
- **search** – Filter and query resources by criteria.
- **inspect** – Compute derived analysis or statistics.
- **draw** – Render a visual image from data (map overlays, plots).

## Important Instructions

- **Do not ask for campaign IDs.** Only the campaigns relevant to the current
  analysis task are accessible through this server.  Use `list_campaigns` to
  discover what is available.
- Call `describe_server_capabilities` to get an overview of all available
  tools before diving into specific operations.
- Start with campaign-level summaries before drilling into individual
  configurations or runs.
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
