# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``robovast-client`` must work with no core installed.

That is the distribution's entire claim, and it is the one property an import check
cannot verify. Every leak found so far was a *deferred* import -- `service_target`
reaching for `robovast.service.client`, `campaign_wait` doing the same, `http_client`
taking a timeout constant from `service.container_exec`. Each module imported perfectly;
each command died the moment it ran, which is the worst place to find out.

So these drive the commands. A verb that would need the core fails here rather than in
front of a user whose install is exactly the one the distribution advertises.

The core is simulated as absent with an import hook rather than by uninstalling it. The
hook matches dotted names and prefixes: an earlier version of a sibling test matched only
top-level packages, so `robovast.service.client` sailed through and proved nothing.
"""

# pylint: disable=redefined-outer-name  # the pytest fixture idiom

import pathlib
import sys

import pytest
from unittest.mock import patch
from click.testing import CliRunner

#: Everything a client install does not ship. `robovast.client`, `robovast.service`'s
#: client half and `robovast.execution.campaign_wait` are deliberately absent from this
#: list -- they *are* the distribution.
CORE_ONLY = (
    "robovast.common",
    "robovast.service.client",
    "robovast.service.local_transport",
    "robovast.service.app",
    "robovast.service.workspaces",
    "robovast.service.container_exec",
    "robovast.execution.controller",
    "robovast.execution.control_server",
    "robovast.execution.backends",
    "robovast.execution.execution_utils",
    "robovast.mcp_server",
)

#: The client distribution's source tree, from which its module list is derived rather
#: than hand-listed -- a hand-listed one silently stops covering a module added later.
CLIENT_SRC = (pathlib.Path(__file__).resolve().parents[2]
              / "src" / "robovast_client" / "robovast")


class _CoreAbsent:
    """Make the core un-importable, as it is in a client-only install."""

    # None is how a MetaPathFinder declines a module
    def find_spec(self, name, path=None, target=None):  # pylint: disable=useless-return
        if any(name == n or name.startswith(n + ".") for n in CORE_ONLY):
            raise ImportError(f"{name}: not installed (client-only)")
        return None


@pytest.fixture
def without_core(monkeypatch):
    finder = _CoreAbsent()
    sys.meta_path.insert(0, finder)
    for loaded in [m for m in sys.modules
                   if any(m == n or m.startswith(n + ".") for n in CORE_ONLY)]:
        monkeypatch.delitem(sys.modules, loaded, raising=False)

    # A real client-only install has no `robovast` *distribution* either, so it
    # contributes no startup hooks and none are expected. Blocking only the modules left
    # the metadata behind, so the CLI correctly concluded the core was installed and then
    # failed loading its hook -- which is the right behaviour for a genuinely stale
    # install, and the wrong simulation of this one.
    from robovast.client import cli as client_cli  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(client_cli, "_core_installed", lambda: False)

    # Entry points are *filtered*, not emptied. Returning `[]` was right while every
    # `cli_plugins` entry belonged to the core; it stopped being right when the client
    # started declaring one of its own (`execution` -> `vast exec`), because emptying the
    # group deleted the very verb this distribution exists to provide and the simulation
    # quietly stopped covering it. So: keep what points into `robovast.client`, drop the
    # rest -- which is exactly what a client-only install's metadata contains.
    real_entry_points = client_cli.entry_points

    def client_only_entry_points(group):
        return [ep for ep in real_entry_points(group=group)
                if ep.value.startswith("robovast.client")]

    monkeypatch.setattr(client_cli, "entry_points", client_only_entry_points)

    yield
    if finder in sys.meta_path:
        sys.meta_path.remove(finder)


def _client_modules():
    for path in sorted(CLIENT_SRC.rglob("*.py")):
        rel = path.relative_to(CLIENT_SRC.parent).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        yield ".".join(parts)


def test_the_module_list_is_not_empty():
    """A guard derived from a directory is worth nothing if the directory moved."""
    mods = list(_client_modules())
    assert len(mods) >= 10, f"only found {mods}; CLIENT_SRC is probably wrong"


@pytest.mark.parametrize("module", sorted(_client_modules()))
def test_every_client_module_imports_without_the_core(without_core, module):
    import importlib  # pylint: disable=import-outside-toplevel
    importlib.import_module(module)


@pytest.mark.parametrize("argv", [
    ["--help"], ["login", "--help"], ["logout", "--help"], ["doctor", "--help"],
    ["workspace", "--help"], ["files", "--help"], ["wait", "--help"],
    ["--version"],
    # The launch path, one level per entry: `exec` is the client's group now, `cluster`
    # is reached through `robovast.exec_plugins`, and `run` is the verb this whole
    # distribution exists to make reachable.
    ["exec", "--help"], ["exec", "cluster", "--help"],
    ["exec", "cluster", "run", "--help"], ["exec", "cluster", "stop", "--help"],
    ["exec", "cluster", "stop-job", "--help"], ["exec", "cluster", "log", "--help"],
    ["exec", "cluster", "download-cleanup", "--help"],
])
def test_the_cli_runs_without_the_core(without_core, argv):
    """`--help` still builds the command and its options, which is where a module-level
    core import would surface."""
    from robovast.client.cli import cli, load_plugins  # pylint: disable=import-outside-toplevel

    # `main()` assembles the CLI; importing it does not. The plugin-provided verbs -- `exec`
    # among them, now that it is the client's -- do not exist on `cli` until this runs, and
    # the `without_core` fixture is what makes it see a client-only install's metadata.
    load_plugins()
    result = CliRunner().invoke(cli, argv)
    assert result.exit_code == 0, result.output


def test_launching_a_campaign_gets_as_far_as_the_service(without_core, monkeypatch,
                                                        tmp_path):
    """`exec cluster run` DRIVEN, not merely `--help`-ed.

    The verb that justifies the whole distribution, and the one whose old home made it
    unreachable. Its body defers four imports (`campaign_wait`, `service.interface`,
    `service.project_push` twice) and any one of them reaching for the core would pass an
    import check and a `--help`, then fail at the only moment that matters. This is the
    class of leak the file exists for: found at call time, not import time.
    """
    import contextlib  # pylint: disable=import-outside-toplevel

    from robovast.client import cluster_cli  # pylint: disable=import-outside-toplevel

    vast = tmp_path / "demo.vast"
    vast.write_text("version: 2\n")
    launched = {}

    def fake_run(client, config_path, **kwargs):  # noqa: ARG001
        launched.update(kwargs, config_path=config_path)
        return "camp-1"

    monkeypatch.setattr("robovast.service.project_push.run_project_via_service", fake_run)

    @contextlib.contextmanager
    def _client(*_a, **_k):
        yield object(), "fake service"

    monkeypatch.setattr(cluster_cli, "service_client", _client)
    monkeypatch.setattr("robovast.client.project_config.ProjectConfig.load",
                        classmethod(lambda cls, start_dir=None: None))

    # `obj={'vast_file': ...}` is the `-V` override, which is how a client-only user names
    # a project: `vast init` is a core verb they do not have.
    result = CliRunner().invoke(cluster_cli.cluster,
                                ["run", "--description", "pilot"],
                                obj={"vast_file": str(vast)})
    assert result.exit_code == 0, result.output
    assert launched["config_path"] == str(vast)
    assert launched["description"] == "pilot"


def test_the_waiting_half_of_wait_and_download_needs_no_core(without_core):
    """``--wait-and-download`` calls `wait_for_campaign_outcome`, which used to be
    `wait_for_cluster_campaign` in the core -- the single reason `run` could not move."""
    from robovast.execution import campaign_wait  # pylint: disable=import-outside-toplevel

    class _Done:
        phase, stage, error, postprocessing_error = "finished", "", "", ""

    class _Client:
        def get_status(self, *_a, **_k):
            return _Done()

    outcome = campaign_wait.wait_for_campaign_outcome(
        "camp-1", client=_Client(), interval=0.0, timeout=10)
    assert outcome == "succeeded"


def test_a_verb_that_talks_to_a_service_gets_that_far(without_core, monkeypatch):
    """The regression that motivated this file.

    `service_client` is the single funnel every service-touching verb goes through, and
    it imported `robovast.service.client` -- the core's re-export of a factory that lives
    in the client. Every one of those verbs died on it, at call time, in exactly the
    install the distribution exists for.
    """
    from robovast.client import service_target  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(service_target, "detected_service_url",
                        lambda: "https://svc.example")
    with service_target.service_client("", None) as (client, label):
        assert client is not None
        assert "svc.example" in label


def test_waiting_builds_its_own_client(without_core, monkeypatch):
    """`campaign_wait` had the same import, so `vast wait` -- the verb a client install
    exists to run -- failed the same way."""
    from robovast.execution import campaign_wait  # pylint: disable=import-outside-toplevel

    class _Done:
        phase, stage, error, postprocessing_error = "finished", "", "", ""

    class _Client:
        def get_status(self, *_a, **_k):
            return _Done()

    # A timeout even though this must not need one. The poll loop treats *any* exception
    # from the client as a transient hiccup and retries, by design -- so a fake with the
    # wrong method name spins forever instead of failing, which is exactly what the first
    # version of this test did to the suite.
    status = campaign_wait.wait_for_campaign_status(
        "camp-1", client=_Client(), interval=0.0, timeout=10)
    assert status.phase == "finished"


def test_doctor_can_ask_about_a_deployment_without_the_core(without_core):
    """`check_deployment` reads the cluster lane, which a client install does not have.

    Its import is deferred inside the function for exactly that reason. A module-level one
    would pass `test_core_without_cluster_package.py` -- that only removes the *lane* --
    and break the install this distribution exists for.
    """
    from robovast.client.doctor import check_deployment  # pylint: disable=import-outside-toplevel

    # The lane is made absent HERE rather than in `CORE_ONLY`, and scoped to this test.
    # Adding it to the fixture's block list evicted `robovast.execution.cluster_execution`
    # from sys.modules for the rest of the session, and other suites hold references into
    # it -- six tests in three other files started failing while passing in isolation.
    #
    # It has to be absent somehow, though: without it the deferred import SUCCEEDS, the
    # code calls the cluster, and returns [] ten seconds later because nothing answered.
    # That is the same answer the ImportError path gives, so this assertion used to hold
    # while proving nothing about it -- and it would have failed on a machine that could
    # reach a deployment. `None` in sys.modules is what makes an import raise.
    with patch.dict(sys.modules, {"robovast.execution.cluster_execution": None}):
        assert check_deployment(namespace="default") == []


def test_no_service_url_and_no_core_is_a_clear_refusal(without_core):
    """With neither a URL nor an in-process server there is nothing to talk to. It must
    say so, not raise ModuleNotFoundError for a module the caller never named."""
    from robovast.service.http_client import \
        RobovastClient  # pylint: disable=import-outside-toplevel

    with pytest.raises(RuntimeError) as excinfo:
        RobovastClient("")
    assert "no in-process service" in str(excinfo.value)
    assert "vast login" in str(excinfo.value)
