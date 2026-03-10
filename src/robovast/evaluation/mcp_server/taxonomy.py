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

"""Server capability and tool taxonomy descriptors.

Provides two introspection helpers that describe the overall MCP server
structure without enumerating every tool in detail.  These are intended
to be registered as MCP tools so that AI clients can orient themselves
before diving into specific operations.
"""

from .registry import get_plugin_tools

VERSION = "1.0"

_RESOURCES = {
    "campaign": "An experiment dataset containing configurations and runs. "
                "Defines the shared input files (scenario, .vast config, run files) "
                "available to every configuration and run.",
    "configuration": "A specific parameterized experiment setup. "
                     "May add configuration-specific files generated "
                     "during variation (stored in _transient).",
    "run": "An individual execution of a configuration. "
           "Inherits all input files from its configuration and campaign. "
           "Produces output files (test results, logs, rosbags).",
    "run_data": "Structured tabular output derived from run artifacts, "
                "primarily CSV files, exposed through query/inspect tools.",
    "artifact": "Files generated or consumed during execution",
}

_OPERATIONS = {
    "get": "Retrieve a specific structured metadata object",
    "list": "Enumerate objects within a resource",
    "query": "Retrieve filtered record sets with pagination",
    "search": "Filter and query resources by criteria",
    "inspect": "Compute derived analysis or statistics",
    "draw": "Render a visual image from data (map overlays, plots)",
}

_NOTES = [
    "A campaign is defined by a .vast configuration file and a .osc "
    "openscenario scenario. Each can include references to other files "
    "that are all located within the same directory tree.",
    "Depending on the campaign configuration, a run might contain many "
    "different result files. To analyze those domain-specific interfaces "
    "are provided.",
    "The tool_groups are discovered dynamically from installed plugins. "
    "Optional packages (robovast-nav, robovast-ros) add additional groups.",
]


def describe_server_capabilities() -> dict:
    """Return a high-level description of the RoboVAST MCP server.

    Covers the resource model, supported operation verbs, and tool groups
    with their member tools.  Tool groups are discovered dynamically from
    the loaded plugins.  This is useful for AI clients that need to
    understand the server layout before calling individual tools.
    """
    tool_groups = get_plugin_tools()
    # Add the server-level tools that are not part of any plugin.
    tool_groups["server_introspection"] = [
        "describe_server_capabilities",
        "describe_tool_taxonomy",
    ]

    return {
        "server": "robovast-results",
        "version": VERSION,
        "resources": _RESOURCES,
        "operations": _OPERATIONS,
        "tool_groups": tool_groups,
        "notes": _NOTES,
    }


def describe_tool_taxonomy() -> dict:
    """Return the verb/resource taxonomy used to name tools.

    Tool names follow the pattern ``<verb>_<resource>[_<detail>]``.
    This helper documents the verbs and resources so that AI clients
    can predict tool names and understand the naming convention.
    """
    return {
        "verbs": {
            "get": "Retrieve structured metadata",
            "list": "Enumerate resources",
            "query": "Retrieve filtered records",
            "search": "Query filtered resources",
            "inspect": "Derived analysis",
            "draw": "Render a visual image",
        },
        "resources": [
            "campaign",
            "configuration",
            "run",
            "run_data",
            "artifact",
        ],
    }
