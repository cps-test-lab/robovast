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

"""MCP plugin: authoring a campaign — the workspace and the ``.vast`` in it.

Everything before a campaign runs. A workspace is the service's only project binding, so
these tools create one, put files in it (through the address space), check the ``.vast``
they wrote, and see what configurations it would expand to. Nothing here starts work.
"""

import json
import logging

from fastmcp import FastMCP

from robovast.mcp_server import service_access
from robovast.mcp_server.service_access import NO_SERVICE

logger = logging.getLogger(__name__)


def create_workspace(name: str = "", from_campaign: str = "") -> dict:
    """Create a workspace — the only project binding a campaign can be started from.

    Holds editable inputs only, and is independent of campaigns: a started campaign is
    self-contained, so editing or deleting the workspace never affects its results.
    Put ``.vast``/``.osc`` in it with ``write_file``, anything else with
    ``create_upload``. A whole directory at once is ``vast workspace init <dir>`` from
    the machine that holds it -- this interface cannot reach your filesystem.

    Args:
        name: Optional human-friendly label.
        from_campaign: Seed it from this campaign's frozen config, to adapt a campaign that
            already ran instead of re-authoring its project. Refuses an incomplete snapshot.

    Returns:
        ``{workspace_id, name, created_at}``.
    """
    from robovast.service.interface import CreateWorkspaceRequest
    try:
        return service_access.client_or_local().create_workspace(
            CreateWorkspaceRequest(name=name, from_campaign=from_campaign)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def list_workspaces(workspace_id: str = "") -> dict:
    """List the workspaces (newest first), or return one.

    Args:
        workspace_id: Return just this workspace. Empty lists all of them.

    Returns:
        ``{workspaces, total}`` of ``{workspace_id, name, created_at, read_only}``,
        or ``{error}``. A ``read_only`` workspace is a directory pinned with
        ``vast serve --workspace-dir``: edit it on the serve host, not through this API.
    """
    try:
        client = service_access.client_or_local()
        if workspace_id:
            found = [client.get_workspace(workspace_id).model_dump()]
        else:
            found = [w.model_dump()
                     for w in client.list_workspaces().workspaces]
        return {"workspaces": found, "total": len(found)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def delete_workspace(workspace_id: str) -> dict:
    """Delete a workspace and its inputs. Existing campaigns are unaffected."""
    try:
        return service_access.client_or_local().delete_workspace(workspace_id).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def create_upload(address: str, executable: bool = False) -> dict:
    """One-time URL to PUT a file whose bytes should not pass through your context.

    For run files, notebooks, postprocessing code and binaries — anything
    ``write_file`` refuses (it takes only ``.vast``/``.osc``). PUT the bytes yourself:
    ``curl -X PUT --data-binary @<file> <url>``.

    Args:
        address: ``/sources/<workspace_id>/<path>`` — the address ``write_file`` takes.
        executable: Set the executable bit (a ``#!`` shebang is also auto-detected).

    Returns:
        ``{token, path, expires_in, url}``; the URL lapses after ``expires_in`` seconds,
        so request a new one rather than reusing a stale grant.
    """
    from robovast.service.interface import CreateUploadRequest
    try:
        return service_access.client_or_local().create_upload(CreateUploadRequest(
            address=address, executable=executable)).model_dump()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


#: Shared note for the two tools that take *address*. Written once: the two make the same
#: choice, and stating it twice is how the two halves drift apart — and every tool
#: description is sent on every request, so a duplicated paragraph is paid for twice.
#:
#: The lane matters beyond tidiness. Against a cluster or ``--attach`` service the
#: workspace is not on this host at all, so a filesystem read would check a different
#: file, or none, and report the verdict as if it were about the one the campaign runs.
_ADDRESS_LANE = """
A ``/sources/<workspace_id>/<path>`` address is checked **through the service**, so this
is the file the campaign will actually run. Anything else is read as a path on the
MCP-server host — for authoring before a workspace exists, and the only lane with no
service running. ``lane`` says which answered.
"""


def _address_lane(address: str):
    """``(workspace_id, rel_path)`` for a ``/sources`` address, or ``None`` for a path.

    Returning ``None`` rather than guessing is the point: an absolute filesystem path
    also starts with ``/``, so the two are told apart by whether the string parses as an
    address in a known namespace — never by a prefix test that would send
    ``/home/me/x.vast`` to the service as workspace ``home``.
    """
    from robovast.client.file_address import RESULTS, SOURCES, AddressError, parse_address
    try:
        namespace, owner, rel_path = parse_address(address)
    except AddressError:
        return None
    if namespace == RESULTS:
        raise ValueError(
            f"{address!r} is a campaign result, which is immutable and is not a project "
            f"to validate. Read it with read_file, or copy it into a /{SOURCES}/ "
            "workspace to work on it.")
    return owner, rel_path


def _unchecked_world_advisory(config_path: str) -> list:
    """Advisory for the local-file lane, which has no service to run a simulator in.

    Only when the project actually declares a simulator backend: a campaign without one has
    no world, and an advisory on every reply is a line callers learn to skip.
    """
    from robovast.common.common import load_config
    from robovast.common.simulators import backend_name
    try:
        parameters = load_config(config_path) or {}
        if not backend_name(parameters.get("execution", {}) or {}):
            return []
    except Exception:  # noqa: BLE001 - a file the checks above already reported on
        return []
    return [{"stage": "world", "config": None,
             "field": "execution.containers.simulation.config",
             "message": "whether this campaign's world loads and compiles was NOT checked: "
                        "that runs the simulator, and this address was read as a plain file "
                        "with no service to run one. Validate through a workspace address "
                        "(/sources/<workspace_id>/<path>) to have it checked."}]


def validate_project(address: str, check_world: bool = True) -> dict:
    """Check a ``.vast`` before running it. Reports **every** problem in one pass.

    Covers YAML, schema, the scenario file and its parameter references, and every plugin
    reference (variation types and their parameters, postprocessing commands, the search
    strategy) — installed entry points and local ``./path.py:Class`` refs alike — each tagged
    with its config block and field, so the file is fixed in as few iterations as it can be.

    ``valid: true`` means the file is well-formed, every reference resolves, and the world
    loads and compiles. It does **not** mean a derived image will build — that failure passes
    validation and then costs a full apt+pip cycle, so if a container adds packages, read
    ``search_docs("build fails schema cannot catch")`` first.

    **The world check is the only one here that runs a container**, and the only one catching a
    world that would fail *every trial* after the image pull. Failures are ``world`` problems
    carrying the simulator's own message. The container is held: a repeat check is ~1.5–2.5 s, a
    cold one ~2–3 s local / 7–15 s cluster. ``check_world=False`` skips it.

    **Each problem names what it could not settle and what would**: an uninstalled ``plugins:``
    spec names ``preview_configurations(limit=1)`` (which composes, and so *installs* them), as
    does a variation needing an aux container — composing is what runs one; a world only an
    unbuilt image could describe names
    ``build_experiment_image``, and says it was **not** checked rather than passing.

    Args:
        address: ``/sources/<workspace_id>/<path>``, or a path on the MCP-server host.

    Returns:
        ``{valid, configs, runs_per_config, total_trials, problems, lane}``, each problem
        ``{stage, config, field, message}``. A clean campaign returns no world entry.
    """
    from robovast.common.config_validation import validate_project_file
    from robovast.service.project_push import _resolve_workspace_id
    try:
        target = _address_lane(address)
        if target is None:
            # No service, so no lane that can run a simulator: say the world went
            # unchecked rather than letting a clean reply read as a checked one.
            report = validate_project_file(address)
            if check_world:
                report = {**report,
                          "problems": list(report.get("problems") or [])
                          + _unchecked_world_advisory(address)}
            return {**report, "lane": "local file"}
        client = service_access.client_or_local()
        workspace_id, rel_path = target
        report = client.validate_project(
            _resolve_workspace_id(client, workspace_id), rel_path, check_world)
        return {**report.model_dump(), "lane": "workspace"}
    except Exception as e:  # noqa: BLE001 - surface any resolution error to the client
        return {"valid": False, "configs": 0, "runs_per_config": 0,
                "total_trials": 0,
                "problems": [{"stage": "project", "config": None,
                              "field": None, "message": str(e)}]}


def preview_configurations(address: str, limit: int = 0) -> dict:
    """What would this ``.vast`` actually run? The resolved cells, without running them.

    ``validate_project`` gives only the counts; this gives each variation cell's resolved
    parameters. Check the sweep here before spending compute on it: no trial runs and
    nothing is written into the project. Not always *free*, though — a variation may need a
    **helper image** to produce what it varies. ``aux_containers`` names what ran; the
    composition is cached, so a later ``start_campaign`` reuses it.

    Args:
        address: ``/sources/<workspace_id>/<path>``, or a path on the MCP-server host.
        limit: Maximum configurations to return; the three totals are always true. **Pass
            ``limit=1`` when the question is only "does this resolve?"** — the cheapest way
            to confirm a ``plugins:`` package installed and its variations expand, which
            ``validate_project`` cannot answer (see there). The default ``0`` means ALL,
            which on a few hundred cells is a large reply for what the first cell already
            gives. ``truncated`` marks a shortened list.

    Returns:
        ``{configs, runs_per_config, total_trials, configurations, truncated,
        aux_containers, lane}``, each configuration ``{name, parameters}``; or ``{error}``.
    """
    from robovast.common.common import load_config
    from robovast.common.config_generation import generate_scenario_variations
    from robovast.service.project_push import _resolve_workspace_id
    try:
        target = _address_lane(address)
        if target is None:
            # A search .vast expands per sampled ParamSet, not from a `configuration:`
            # block; composing a sample is the only preview that reflects what it runs.
            aux: list = []
            if (load_config(address) or {}).get("search"):
                from robovast.search.compose import preview_search_sample
                sample = preview_search_sample(address)
                configs = sample["configs"]
                runs = sample["runs_per_config"]
            else:
                campaign_data = generate_scenario_variations(
                    variation_file=address, output_dir=None)
                configs = campaign_data["configs"]
                runs = campaign_data.get("execution", {}).get("runs", 1)
                aux = list(campaign_data.get("aux_containers") or [])
            items = [{"name": c["name"], "parameters": c.get("config", {})}
                     for c in configs]
            truncated = bool(limit) and len(items) > limit
            return {
                "configs": len(configs),
                "runs_per_config": runs,
                "total_trials": len(configs) * runs,
                "configurations": items[:limit] if truncated else items,
                "truncated": truncated,
                "aux_containers": aux,
                "lane": "local file",
            }
        client = service_access.client_or_local()
        workspace_id, rel_path = target
        resp = client.preview_configurations(
            _resolve_workspace_id(client, workspace_id), limit, rel_path)
        # ``previews`` carries the web UI's Module-Federation asset refs for rendering a
        # variation; they are useless to an MCP caller and would be the bulk of the reply.
        return {
            "configs": resp.configs,
            "runs_per_config": resp.runs_per_config,
            "total_trials": resp.total_trials,
            "configurations": [{"name": c.name, "parameters": c.parameters}
                               for c in resp.configurations],
            "truncated": resp.truncated,
            "aux_containers": list(resp.aux_containers),
            "lane": "workspace",
        }
    except Exception as e:  # noqa: BLE001 - surface any resolution error to the client
        # error_result rather than {"error": str(e)}: composing here can refuse with an
        # ActionableError (a variation needing an auxiliary container no runner can provide),
        # and dropping its next_step leaves the caller with a reason and no move.
        return service_access.error_result(e)


def describe_world(address: str, targets: str = "", entities: bool = False,
                   backend: str = "") -> dict:
    """What does this campaign's world offer an override? Asked of the simulator itself.

    Which components a ``sim`` override can address, and with ``targets`` which model values a run
    may change (friction, contact masks, force limits, mass) and the objects that can be named.
    Guess either and the run is refused in the container, after the image pull. Asked in the
    image the campaign runs — a world ref resolves to what is installed there — and the reply
    names it.

    Args:
        targets: Glob over object names, e.g. ``'gripper_right*'``. Empty reports the
            overridable *fields* only and builds no model; a glob builds one, as does
            *entities*.
        backend: ``"local"`` or ``"cluster"``; the cluster lane refuses this query today and
            says why.

    Returns:
        ``{backend, image, duration_s, world, packaged, inputs, components, entities, overridable,
        dropped_transport, errors}``, ``overridable`` being ``{fields, targets}``; or
        ``{error}`` — including when only an unbuilt image could answer. A non-empty ``errors``
        means a partial answer: read it before taking a null ``entities`` for a world that
        compiles none. ``dropped_transport`` names transport plugins the build left out, which a
        describe does not need.
    """
    from robovast.service.project_push import _resolve_workspace_id
    try:
        target = _address_lane(address)
        if target is None:
            raise ValueError(
                "describe_world needs a workspace address (/sources/<workspace_id>/<path>): "
                "the world is described by the campaign's own image, which only the service "
                "knows how to reach")
        client = service_access.client_or_local()
        workspace_id, rel_path = target
        described = client.describe_world(
            _resolve_workspace_id(client, workspace_id), rel_path, targets, entities, backend)
        return described.model_dump()
    except Exception as e:  # noqa: BLE001 - surface any resolution error to the client
        return {"error": str(e)}


def _resolved_request(address: str):
    """*address* -> ``(client, ExecRequest)`` with no ``command`` set yet, or raise
    ``ValueError`` naming why (no address, no service).
    """
    from robovast.service.interface import ExecRequest
    from robovast.service.project_push import _resolve_workspace_id
    target = _address_lane(address)
    if target is None:
        raise ValueError(
            "this needs a workspace address (/sources/<workspace_id>/<path>): the answer "
            "comes from the campaign's own image, which only the service knows how to reach")
    client = service_access.service_client()
    if client is None:
        raise ValueError(NO_SERVICE)
    workspace_id, rel_path = target
    resolved_id = _resolve_workspace_id(client, workspace_id)
    return client, ExecRequest(workspace_id=resolved_id, config_path=rel_path)


def _exec_json(client, request, command: str, container: str = "") -> dict:
    """*request* run with *command*, via ``exec_in_container``'s own lane-agnostic
    plumbing -- not ``describe_world``'s ``_make_container_runner`` path, which only
    gets a cluster-capable runner inside a live campaign's composition and is refused
    standalone on the cluster lane. Returns the command's parsed stdout, or raises
    ``ValueError`` naming why (a non-zero exit, unparseable output).

    Always ``query=True``: these are read-only questions put to an image, so they run in
    the service's query pool. Two reasons. A one-shot exec discards the held container by
    design, so a call here would destroy the container its caller is debugging in. And the
    pool *holds* the container, so a second
    question about the same project costs an exec rather than a container start -- measured
    at ~0.5 s against 6-15 s on the cluster lane.

    *container* names which one answers, because they are different images: ``roqsim``
    lives in the simulator's and ``scenario_execution`` in the scenario's.
    """
    update = {"command": command, "query": True}
    if container:
        update["container"] = container
    result = client.exec_in_container(request.model_copy(update=update))
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout or "").strip()[:400]
        raise ValueError(f"{command!r} failed: {detail or '(no output)'}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"{command!r} produced unparseable output: {e}") from e


def describe_scenario(address: str, scenario_path: str) -> dict:
    """What a ``.osc`` file references and does.

    Returns ``{valid, diagnostics, actions_used, tree, image}``.
    """
    try:
        client, request = _resolved_request(address)
        container_path = f"/sources/{request.workspace_id}/{scenario_path}"
        payload = _exec_json(
            client, request,
            # ``python3``: a declared base image has no ``python`` (see image_catalog's
            # ``_COMMANDS``), so this failed for every project that does not build its
            # scenario image -- which is most of them.
            f"python3 -m scenario_execution.introspection describe {container_path}",
            container="scenario")
        image = client.resolve_image(
            request.model_copy(update={"container": "scenario"})).image
        return {**payload, "image": image}
    except Exception as e:  # noqa: BLE001 - surface any resolution error to the client
        return {"error": str(e)}


def get_world_body_tree(address: str, world_path: str, pattern: str) -> dict:
    """Body hierarchy under bodies matching the required glob `pattern`, capped per
    match. `{bodies: [{root, tree, truncated}], image}`.
    """
    try:
        if not pattern:
            raise ValueError("pattern is required -- there is no 'describe every body' mode")
        client, request = _resolved_request(address)
        container_path = f"/sources/{request.workspace_id}/{world_path}"
        payload = _exec_json(
            client, request,
            # No `--json`: `roqsim scenes describe` has no such flag and argparse refuses the whole
            # command over it (its answer is JSON either way), so this tool could never once have
            # succeeded against a real image. The stub in its test made the mistake invisible.
            f"roqsim scenes describe {container_path} --body-tree {pattern}",
            # The SIMULATOR's image, which is the only one with roqsim in it. Unqualified,
            # this resolved to the scenario container -- so on any project whose simulator
            # comes from the image family it answered "roqsim: command not found", and the
            # tool had never worked there.
            container="simulation")
        image = client.resolve_image(
            request.model_copy(update={"container": "simulation"})).image
        return {"bodies": payload.get("body_tree") or [], "image": image}
    except Exception as e:  # noqa: BLE001 - surface any resolution error to the client
        return {"error": str(e)}


for _fn in (validate_project, preview_configurations, describe_world):
    _fn.__doc__ = _fn.__doc__.replace(
        "    Args:\n", f"{_ADDRESS_LANE}\n    Args:\n", 1)


# -- Plugin class ------------------------------------------------------------

# A workspace's *files* are not here: they are written and read through the one address
# space (``write_file`` / ``read_file`` over ``/sources/<workspace_id>/<path>``), so there
# is a single way to name a file rather than one per scope.

_TOOLS = [
    create_workspace,
    list_workspaces,
    delete_workspace,
    create_upload,
    validate_project,
    preview_configurations,
    describe_world,
    describe_scenario,
    get_world_body_tree,
]


class AuthoringPlugin:
    """MCP plugin: authoring a campaign — the workspace and the ``.vast`` in it."""

    name = "authoring"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
