# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast login`` — where the credentials live, and which service a command talks to.

Two properties carry the design:

* the token file is created **0600 from the start**, not chmod-ed afterwards — between
  the two there is a window where a secret is world-readable;
* the local port still wins over a stored login, so starting a ``vast serve`` on this
  machine keeps redirecting every command to it, exactly as before this existed.
"""

import json
import os
import stat

import pytest

from robovast.client import login, service_target

# The login config is isolated for every test by an autouse fixture in tests/conftest.py.
# It lived here first, which is how test_service_target.py came to read the maintainer's
# real credentials -- per-module isolation only protects the module that thought of it.


def _runner():
    """A CliRunner that cannot see the developer's ``./.env``.

    ``vast``'s group callback loads ``./.env`` from the working directory into
    ``os.environ``, once, for every command. Invoking the real CLI from a test therefore
    leaks whatever the maintainer has configured -- registry credentials, share settings
    -- into the rest of the session, and unrelated suites later assert on manifests built
    from those variables. Running from an empty directory keeps a CLI test to its own
    subject.
    """
    from click.testing import CliRunner
    return CliRunner()


def test_nothing_stored_reads_as_logged_out():
    assert login.load() == {}
    assert login.credentials() == ("", "", "")


def test_save_then_load_round_trips():
    login.save("https://robovast.example.org/", "tok", "Fred")
    assert login.credentials() == ("https://robovast.example.org", "tok", "Fred")


def test_the_token_file_is_private():
    path = login.save("https://robovast.example.org", "tok", "Fred")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, oct(mode)


def test_the_stored_shape_leaves_room_for_a_real_identity_provider():
    """``auth`` is an object so ``type``/``expires_at`` can arrive without a migration."""
    path = login.save("https://robovast.example.org", "tok", "Fred")
    payload = json.loads(path.read_text())
    assert payload["auth"]["type"] == "shared-secret"
    assert payload["auth"]["token"] == "tok"


def test_a_corrupt_config_reads_as_logged_out():
    """An unreadable config must not make every command, including --help, fail."""
    path = login.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert login.load() == {}


def test_clear_forgets_the_login():
    login.save("https://robovast.example.org", "tok", "Fred")
    assert login.clear() is True
    assert login.credentials() == ("", "", "")
    assert login.clear() is False


def test_an_empty_name_is_kept_empty():
    """"No name" is a real answer, not a prompt to invent one."""
    login.save("https://robovast.example.org", "tok", "")
    assert login.credentials()[2] == ""


def test_a_local_service_wins_over_a_stored_login(monkeypatch):
    """The dev workflow is unchanged: a service on this machine is what commands follow."""
    login.save("https://robovast.example.org", "tok", "Fred")
    monkeypatch.setattr(service_target, "_service_alive", lambda url: True)
    assert service_target.detected_service_url() == "http://127.0.0.1:8800"


def test_the_stored_login_is_used_when_nothing_serves_locally(monkeypatch):
    login.save("https://robovast.example.org", "tok", "Fred")
    monkeypatch.setattr(service_target, "_service_alive", lambda url: False)
    assert service_target.detected_service_url() == "https://robovast.example.org"


def test_no_service_and_no_login_resolves_to_nothing(monkeypatch):
    monkeypatch.setattr(service_target, "_service_alive", lambda url: False)
    assert service_target.detected_service_url() == ""


def test_the_client_picks_up_the_stored_credentials(monkeypatch):
    """Every construction site gets them without threading them through by hand."""
    from robovast.service.client import RobovastClient
    login.save("https://robovast.example.org", "tok", "Fred")
    client = RobovastClient("https://robovast.example.org")
    assert client.session.headers["Authorization"] == "Bearer tok"
    assert client.session.headers["X-Robovast-User"] == "Fred"


def test_explicit_credentials_override_the_stored_ones():
    from robovast.service.client import RobovastClient
    login.save("https://robovast.example.org", "tok", "Fred")
    client = RobovastClient("https://elsewhere.example.org", token="other", user="Ada")
    assert client.session.headers["Authorization"] == "Bearer other"
    assert client.session.headers["X-Robovast-User"] == "Ada"


def test_the_login_command_verifies_before_it_stores(monkeypatch):
    """A typo must fail at ``vast login``, not as a 401 from the next unrelated command."""
    from robovast.client.cli import cli

    class _Refuses:
        def version(self):
            raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr("robovast.service.http_client.RobovastClient",
                        lambda *a, **kw: _Refuses())
    runner = _runner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli, ["login", "https://robovast.example.org", "--token", "wrong", "--name", "Fred"])

    assert result.exit_code != 0
    assert "could not reach" in result.output
    # A 401 is the one failure where blaming the token is right; see _login_remedy,
    # which distinguishes it from an untrusted certificate and an address that never
    # answered, because those two are not about the token at all.
    assert "rejected the token" in result.output
    # Nothing was written: a rejected token must not become the stored one.
    assert login.credentials() == ("", "", "")


def test_the_login_command_stores_verified_credentials(monkeypatch):
    from robovast.client.cli import cli

    class _Accepts:
        def version(self):
            return object()

    monkeypatch.setattr("robovast.service.http_client.RobovastClient",
                        lambda *a, **kw: _Accepts())
    runner = _runner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli, ["login", "https://robovast.example.org", "--token", "tok", "--name", "Fred"])

    assert result.exit_code == 0, result.output
    assert login.credentials() == ("https://robovast.example.org", "tok", "Fred")


def test_the_login_command_prints_the_mcp_registration(monkeypatch):
    """An agent over HTTP reads no config file, so login has to spell out all three facts.

    The name especially: without that header the agent's campaigns arrive unattributed
    while the same person's CLI runs are labelled, and nothing anywhere says why.
    """
    from robovast.client.cli import cli

    class _Accepts:
        def version(self):
            return object()

    monkeypatch.setattr("robovast.service.http_client.RobovastClient",
                        lambda *a, **kw: _Accepts())
    runner = _runner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli, ["login", "https://robovast.example.org", "--token", "tok",
                  "--name", "Fred Pasch"])

    assert result.exit_code == 0, result.output
    command = " ".join(result.output.replace("\\\n", " ").split())
    assert ("claude mcp add --transport http robovast "
            "https://robovast.example.org/mcp") in command
    assert "--header 'Authorization: Bearer tok'" in command
    # Quoted as one argument: a name with a space in it is the common case, not an edge.
    assert "--header 'X-Robovast-User: Fred Pasch'" in command


def test_the_mcp_registration_omits_a_name_nobody_gave():
    """Empty means unattributed; an empty header would claim someone called themselves ''."""
    lines = login.mcp_add_command("https://robovast.example.org", "tok", "")
    assert not any("X-Robovast-User" in line for line in lines)
    assert lines == ["claude mcp add --transport http robovast "
                     "https://robovast.example.org/mcp",
                     "--header 'Authorization: Bearer tok'"]


def test_logout_forgets_them(monkeypatch):
    from robovast.client.cli import cli

    login.save("https://robovast.example.org", "tok", "Fred")
    runner = _runner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["logout"])
    assert result.exit_code == 0, result.output
    assert login.credentials() == ("", "", "")
