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

"""Package-provided **service data endpoints**.

A package installs a run-scoped JSON data endpoint served at
``GET /campaigns/{id}/<name>?config_name=…&run_id=…&…`` by registering a class in the
``robovast.service_endpoints`` entry-point group — no core edit, and no frontend change
(the run-view already reaches any such endpoint through ``data.fetchRun(name, params)``).

This closes the last core-coupling for a self-contained analysis package: it can ship a
**postprocessing** plugin (writes a table into ``data.db``), a **service endpoint** (this
module — serves that table as JSON), and a **panel** (renders it) entirely via entry points.
``robovast_nav``'s ``costmap`` is the reference across all three.

Design mirrors the MCP-plugin loader (:mod:`robovast.mcp_server.registry`): an entry-point
group + a small ``Protocol``. A handler receives a :class:`RunDataContext` (a typed facade —
``open_db()`` / ``run_dir()`` / ``params``) so it never hardcodes the on-disk layout, and the
host resolves the campaign dir behind it (local disk or, on the cluster, an object-store
fetch), giving local/cluster transparency for free.

Scope (deliberately narrow): run-scoped **GET → JSON**. Binary/large per-run artifacts are
already served generically by ``GET /results/<campaign>/<config>/<run>/<path>`` — use that,
not this. Producing the data is a postprocessing plugin's job; this only serves it.
"""

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Entry-point group for package-provided service data endpoints.
ENDPOINT_GROUP = "robovast.service_endpoints"

#: First path segments already owned by core ``/campaigns/{id}/<X>`` routes. A plugin
#: endpoint whose name starts with one of these is skipped (it would shadow a core route,
#: which registers first and would win anyway). ``costmap`` is intentionally absent — its
#: core route was removed, freeing the name for the ``robovast_nav`` plugin.
RESERVED_CAMPAIGN_ENDPOINTS = frozenset({
    "status", "stop", "describe", "query", "data-status", "plots", "panels",
    "visualizations", "notebook", "archive", "postprocessing", "panel_assets",
})


@dataclass
class RunDataContext:
    """What a service-endpoint handler receives — a typed facade over one campaign's data,
    so handlers never touch the on-disk layout or the local/cluster distinction.

    ``params`` is the request's query string (always includes ``config_name`` + ``run_id``
    for a run-scoped endpoint, plus any endpoint-specific params). ``data_dir`` is resolved
    by the host (local disk, or an object-store fetch on the cluster) — prefer the helpers.
    """

    campaign_id: str
    params: Mapping[str, str]
    data_dir: str

    @property
    def config_name(self) -> str:
        """The run's config name (query param). ``ValueError`` (→ 400) if absent."""
        v = self.params.get("config_name")
        if v is None:
            raise ValueError("missing required query param 'config_name'")
        return v

    @property
    def run_id(self) -> int:
        """The run id (query param), as int. ``ValueError`` (→ 400) if absent/invalid."""
        v = self.params.get("run_id")
        if v is None:
            raise ValueError("missing required query param 'run_id'")
        try:
            return int(v)
        except (TypeError, ValueError) as e:
            raise ValueError(f"run_id must be an integer, got {v!r}") from e

    @contextmanager
    def open_db(self):
        """A **read-only** sqlite connection over the campaign's ``data.db`` (with
        ``campaign.db`` attached as schema ``campaign``), auto-closed on exit. Raises
        :class:`~robovast.results_processing.data_query.DataQueryError` (→ 400) when the
        campaign has no database yet (postprocessing hasn't run)."""
        from robovast.results_processing.data_query import \
            open_data_db  # pylint: disable=import-outside-toplevel
        conn = open_data_db(self.data_dir)
        try:
            yield conn
        finally:
            conn.close()

    def run_dir(self, config_name: str, run_id) -> Path:
        """Absolute path to one run's artifact directory
        (``<data_dir>/<config_name>/<run_id>``), confined to the campaign dir. Raises
        ``ValueError`` (→ 400) on a ``..``/absolute escape. (For serving a run artifact
        *file* prefer the ``/results`` address space; this is for handlers that need to
        read run artifacts to compute a JSON response.)"""
        base = Path(self.data_dir).resolve()
        target = (base / str(config_name) / str(run_id)).resolve()
        if target != base and not str(target).startswith(str(base) + os.sep):
            raise ValueError("run path escapes the campaign directory")
        return target


@runtime_checkable
class ServiceEndpoint(Protocol):
    """A package-provided run-scoped data endpoint.

    ``name`` is the URL segment (== what a panel passes to ``fetchRun``); prefer a
    package-namespaced name (``"nav/costmap"``) so packages never collide. ``handle``
    returns a JSON-serializable object (dict / pydantic model / ``None``); raising
    ``KeyError`` → 404 and ``ValueError``/``DataQueryError`` → 400 via the service's guard.
    """

    name: str

    def handle(self, ctx: RunDataContext) -> Any:
        ...


def load_service_endpoints() -> "dict[str, ServiceEndpoint]":
    """Discover installed ``robovast.service_endpoints`` plugins, keyed by endpoint name.

    Best-effort (a broken plugin is logged and skipped), and it skips any name that would
    shadow a core route (:data:`RESERVED_CAMPAIGN_ENDPOINTS`) or duplicate an already-loaded
    endpoint — mirrors :func:`robovast.mcp_server.registry.load_plugins`.
    """
    endpoints: "dict[str, ServiceEndpoint]" = {}
    for ep in entry_points(group=ENDPOINT_GROUP):
        try:
            obj = ep.load()
            inst = obj() if isinstance(obj, type) else obj
            if not isinstance(inst, ServiceEndpoint):
                logger.warning(
                    "service endpoint %r does not satisfy ServiceEndpoint protocol; skipped",
                    ep.name)
                continue
            name = inst.name
            if name.split("/", 1)[0] in RESERVED_CAMPAIGN_ENDPOINTS:
                logger.warning(
                    "service endpoint %r shadows a core campaign route; skipped", name)
                continue
            if name in endpoints:
                logger.warning(
                    "service endpoint name %r already registered; skipping duplicate from %r",
                    name, ep.value)
                continue
            endpoints[name] = inst
            logger.debug("Loaded service endpoint %r from %r.", name, ep.value)
        except Exception:  # noqa: BLE001 - one broken plugin must not break the service
            logger.exception("Failed to load service endpoint from entry point %r.", ep.name)
    return endpoints
