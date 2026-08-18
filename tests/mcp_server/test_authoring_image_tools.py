# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``describe_scenario`` and ``get_world_body_tree`` -- the usage-side
counterparts to ``describe_world``, for a ``.osc`` file and a world's body hierarchy.

Both reuse ``exec_in_container``'s lane-agnostic plumbing (not ``describe_world``'s own
``_make_container_runner`` path), and neither is cached -- their answer depends on the
named file's own content, not just the image, the same as ``describe_world`` itself.
"""

import json
import re

import pytest

from robovast.mcp_server import service_access
from robovast.mcp_server.plugins import authoring
from robovast.service.interface import ExecResult, ImageResolution


class _FakeClient:
    def __init__(self, image="robovast-build:abc123", payload=None, exit_code=0, stderr=""):
        self.exec_calls = []
        self.resolve_calls = []
        self.image = image
        self.payload = payload if payload is not None else {"valid": True}
        self.exit_code = exit_code
        self.stderr = stderr

    def resolve_image(self, request):
        self.resolve_calls.append(request)
        return ImageResolution(image=self.image)

    def exec_in_container(self, request):
        self.exec_calls.append(request)
        return ExecResult(exit_code=self.exit_code, stdout=json.dumps(self.payload),
                          stderr=self.stderr)


@pytest.fixture
def service(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    return fake


# -- describe_scenario ----------------------------------------------------------


def test_describe_scenario_needs_a_workspace_address():
    out = authoring.describe_scenario(address="/home/me/x.vast", scenario_path="s.osc")
    assert "workspace address" in out["error"]


def test_describe_scenario_no_service_is_reported(monkeypatch):
    monkeypatch.setattr(service_access, "service_client", lambda: None)
    out = authoring.describe_scenario(address="/sources/ws-1/a.vast", scenario_path="s.osc")
    assert "error" in out


def test_describe_scenario_runs_the_introspection_module_at_the_sources_address(service):
    authoring.describe_scenario(
        address="/sources/ws-1/a.vast", scenario_path="scenarios/pick.osc")
    request = service.exec_calls[-1]
    assert "python3 -m scenario_execution.introspection describe" in request.command
    assert "/sources/ws-1/scenarios/pick.osc" in request.command


def test_the_interpreter_is_one_a_declared_image_actually_has(service):
    """``python3``, never bare ``python``.

    A DECLARED base image has no ``python`` -- Debian ships only ``python3`` (PEP 394) -- while
    an image RoboVAST *built* does, because the venv at /usr/local provides one. So the bare
    form worked for a project that builds its scenario image and failed with "python: command
    not found" for every project that declares one, which is the common case.

    This assertion is the narrow thing a stubbed test CAN check. It could not have caught the
    original bug: the stub answers whatever is asked of it, so a command naming an interpreter
    that does not exist in any real image passed here for as long as it was wrong. What catches
    that is an exec against a declared-image container, which is a live check rather than this
    one -- worth knowing before trusting this test to protect the behaviour.
    """
    authoring.describe_scenario(address="/sources/ws-1/a.vast", scenario_path="s.osc")
    command = service.exec_calls[-1].command
    assert "python3 -m" in command
    assert not re.search(r"(?<!\w)python(?!\d)", command), command


def test_describe_scenario_returns_the_payload_plus_the_resolved_image(service):
    service.payload = {
        "valid": False,
        "diagnostics": [{"severity": "error", "line": 4, "column": 8,
                         "message": "unknown action 'x'", "file": "s.osc", "phase": "semantic"}],
        "actions_used": [{"name": "x", "kind": "action", "doc": None, "resolvable": False}],
        "tree": None,
    }
    out = authoring.describe_scenario(address="/sources/ws-1/a.vast", scenario_path="s.osc")
    assert out["valid"] is False
    assert out["actions_used"][0]["name"] == "x"
    assert out["tree"] is None
    assert out["image"] == service.image


def test_describe_scenario_a_nonzero_exit_is_reported_as_error(monkeypatch):
    fake = _FakeClient(exit_code=1, stderr="ModuleNotFoundError")
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    out = authoring.describe_scenario(address="/sources/ws-1/a.vast", scenario_path="s.osc")
    assert "error" in out
    assert "ModuleNotFoundError" in out["error"]


def test_describe_scenario_is_not_cached_between_calls(service):
    """Unlike the catalog tools -- the answer depends on the file's own content."""
    authoring.describe_scenario(address="/sources/ws-1/a.vast", scenario_path="s.osc")
    authoring.describe_scenario(address="/sources/ws-1/a.vast", scenario_path="s.osc")
    assert len(service.exec_calls) == 2


# -- get_world_body_tree ----------------------------------------------------------


def test_get_world_body_tree_pattern_is_required(service):
    out = authoring.get_world_body_tree(
        address="/sources/ws-1/a.vast", world_path="w.yaml", pattern="")
    assert "pattern is required" in out["error"]
    assert not service.exec_calls


def test_get_world_body_tree_needs_a_workspace_address():
    out = authoring.get_world_body_tree(
        address="/home/me/x.vast", world_path="w.yaml", pattern="gripper*")
    assert "workspace address" in out["error"]


def test_get_world_body_tree_runs_rst_scenes_describe_with_the_flag(service):
    service.payload = {"body_tree": []}
    authoring.get_world_body_tree(
        address="/sources/ws-1/a.vast", world_path="worlds/depot.yaml", pattern="gripper*")
    request = service.exec_calls[-1]
    assert "roqsim scenes describe" in request.command
    assert "/sources/ws-1/worlds/depot.yaml" in request.command
    assert "--body-tree gripper*" in request.command
    # Only flags the CLI actually has. This used to assert `--json`, which `roqsim scenes describe`
    # rejects -- argparse refuses the whole command over an unknown flag, so the tool failed
    # against every real image while passing here against a stub.
    assert "--json" not in request.command


def test_get_world_body_tree_extracts_the_body_tree_key_and_the_image(service):
    service.payload = {
        "world": "...", "body_tree": [
            {"root": "gripper_right", "truncated": False,
             "tree": {"name": "gripper_right", "type": "body"}},
        ],
    }
    out = authoring.get_world_body_tree(
        address="/sources/ws-1/a.vast", world_path="w.yaml", pattern="gripper*")
    assert out["bodies"] == service.payload["body_tree"]
    assert out["image"] == service.image


def test_get_world_body_tree_missing_key_defaults_to_empty_list(service):
    service.payload = {"world": "..."}  # older/odd payload with no body_tree key at all
    out = authoring.get_world_body_tree(
        address="/sources/ws-1/a.vast", world_path="w.yaml", pattern="gripper*")
    assert out["bodies"] == []


def test_get_world_body_tree_a_nonzero_exit_is_reported_as_error(monkeypatch):
    fake = _FakeClient(exit_code=1, stderr="cannot build world")
    monkeypatch.setattr(service_access, "service_client", lambda: fake)
    out = authoring.get_world_body_tree(
        address="/sources/ws-1/a.vast", world_path="w.yaml", pattern="gripper*")
    assert "error" in out
    assert "cannot build world" in out["error"]
