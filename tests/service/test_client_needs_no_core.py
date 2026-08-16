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

    def find_spec(self, name, path=None, target=None):
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
])
def test_the_cli_runs_without_the_core(without_core, argv):
    """`--help` still builds the command and its options, which is where a module-level
    core import would surface."""
    from robovast.client.cli import cli  # pylint: disable=import-outside-toplevel

    result = CliRunner().invoke(cli, argv)
    assert result.exit_code == 0, result.output


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
    from robovast.execution import \
        campaign_wait  # pylint: disable=import-outside-toplevel

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


def test_no_service_url_and_no_core_is_a_clear_refusal(without_core):
    """With neither a URL nor an in-process server there is nothing to talk to. It must
    say so, not raise ModuleNotFoundError for a module the caller never named."""
    from robovast.service.http_client import \
        RobovastClient  # pylint: disable=import-outside-toplevel

    with pytest.raises(RuntimeError) as excinfo:
        RobovastClient("")
    assert "no in-process service" in str(excinfo.value)
    assert "vast login" in str(excinfo.value)
