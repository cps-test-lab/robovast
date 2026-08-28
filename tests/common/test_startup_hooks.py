# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``./.env`` is read once per invocation, and its absence is never silent.

The root command group lives in the client distribution, which must not import the
core -- so the ``.env`` read is *contributed* by the core through the
``robovast.cli_startup`` entry-point group rather than imported.

That indirection has a failure mode worth a test of its own. Entry points are baked
into a distribution's installed metadata, so adding one to ``pyproject.toml`` does
nothing until the package is reinstalled, and an editable checkout looks entirely
normal in the meantime. It happened: with the core installed but its hook
unregistered, no ``.env`` was read, and ``vast service upgrade`` -- which
reconciles Secrets from the environment -- concluded the registry and git
credentials "configuration is gone" and **deleted both**. A silently-unread config
file is not a degraded feature; it is a wrong answer with consequences.
"""

# pylint: disable=redefined-outer-name  # the pytest fixture idiom

import click
import pytest

from robovast.client import cli as client_cli


@pytest.fixture
def hooks(monkeypatch):
    """Control what the startup-hook group appears to contain."""
    def _install(entries, core=True):
        monkeypatch.setattr(client_cli, "entry_points", lambda group: list(entries))
        monkeypatch.setattr(client_cli, "_core_installed", lambda: core)
    return _install


class _Hook:
    def __init__(self, fn, name="env-file"):
        self.name, self._fn = name, fn

    def load(self):
        return self._fn


def test_the_env_hook_runs(hooks):
    ran = []
    hooks([_Hook(lambda: ran.append(True))])

    client_cli.run_startup_hooks()
    assert ran == [True]


def test_a_core_install_with_no_hooks_says_so(hooks):
    """The regression. Silence here means every image pin and credential in ``.env`` is
    invisible, and the command runs on as if the file were empty."""
    hooks([], core=True)

    with pytest.raises(click.ClickException) as excinfo:
        client_cli.run_startup_hooks()
    message = str(excinfo.value)
    assert ".env" in message, "must name what went unread"
    assert "pip install -e ." in message, "must name the fix"


def test_a_client_only_install_expects_no_hooks(hooks):
    """Nothing a ``.env`` carries is consumed by a client, so contributing no hook is
    correct there -- and must not be reported as a broken install."""
    hooks([], core=False)

    client_cli.run_startup_hooks()  # must not raise


def test_an_unusable_env_file_is_a_clean_error(hooks):
    """``load_env_file`` raises ValueError listing every ``*_FILE`` entry naming a file
    that is not there. That is a user error with an actionable message, not a crash."""
    def _bad():
        raise ValueError("/p/.env names files that cannot be used:\n  - X_FILE='no'")

    hooks([_Hook(_bad)])

    with pytest.raises(click.ClickException) as excinfo:
        client_cli.run_startup_hooks()
    assert "cannot be used" in str(excinfo.value)


def test_any_other_hook_failure_propagates(hooks):
    """A hook is installed capability, not an optional extra. Swallowing a broken one
    gives a CLI that behaves subtly differently with nothing said."""
    def _boom():
        raise RuntimeError("hook is broken")

    hooks([_Hook(_boom)])

    with pytest.raises(RuntimeError):
        client_cli.run_startup_hooks()


def test_the_core_really_registers_the_hook():
    """The end-to-end version: this install must actually have it. Catches exactly the
    stale-metadata case the fixtures above can only simulate."""
    from importlib.metadata import entry_points

    registered = {ep.name: ep.value
                  for ep in entry_points(group=client_cli.STARTUP_HOOK_GROUP)}
    assert registered.get("env-file") == "robovast.common.env_file:load_env_file", (
        f"the core's .env hook is not registered (found {registered}); "
        f"re-run 'pip install -e .' in the robovast checkout")
