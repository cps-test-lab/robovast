# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast exec cluster token``: read the token back without kubectl syntax.

Setup prints the token once, on purpose. Reading it again meant

    kubectl get secret robovast-auth -o jsonpath='{.data.ROBOVAST_AUTH_TOKEN}' | base64 -d

which answers a RoboVAST question in kubectl's language and lands in every operator's
shell history.

The failure it is most likely to prevent is subtler than convenience: **a token is per
cluster**. Reading one instance's secret and trying it against another produces "That
token was not accepted", which is indistinguishable from a typo -- so the command prints
the URL the token belongs to right next to it.
"""

from unittest.mock import patch

from click.testing import CliRunner

from robovast.execution.cluster_execution.cli import cluster_token

TOKEN = "a-token-that-is-long-enough-to-look-real"
MODULE = "robovast.execution.cluster_execution.service_deploy"


def _run(args, token=TOKEN, url="https://robovast.example.org"):
    with patch(f"{MODULE}.existing_auth_token", return_value=token), \
         patch(f"{MODULE}.published_url", return_value=url):
        # The command itself, not `cluster token`: the group lives in robovast-client and
        # resolves this through entry-point metadata, which would make these unit tests
        # depend on a fresh reinstall. `test_operator_hands_out_the_client` covers the
        # chain deliberately.
        return CliRunner().invoke(cluster_token, args)


def test_quiet_prints_the_token_and_nothing_else():
    """It must be pipeable: any decoration would end up in whatever consumes it."""
    result = _run(["-q"])
    assert result.exit_code == 0
    assert result.output.strip() == TOKEN


def test_the_handout_names_the_url_the_token_belongs_to():
    """A token without its URL is what makes the per-cluster mixup unrecoverable."""
    result = _run([])
    assert result.exit_code == 0
    assert "https://robovast.example.org" in result.output
    assert TOKEN in result.output
    # The three ways in, so the operator sends one message rather than three.
    assert "vast login" in result.output
    assert "/mcp" in result.output
    assert "Browser" in result.output


def test_an_unpublished_service_still_yields_its_token():
    """No Ingress is a real state, and the token is still the right answer there."""
    result = _run([], url="")
    assert result.exit_code == 0
    assert TOKEN in result.output
    assert "--ingress-host" in result.output, "it should say how to publish it"


def test_no_token_is_an_error_that_names_the_fix():
    """Silence here would read as "there is no password", which is never true."""
    result = _run([], token="")
    assert result.exit_code != 0
    assert "setup" in result.output


def test_setup_points_at_this_command_rather_than_kubectl():
    """The two must not drift: setup's parting words are where operators look first."""
    import inspect

    from robovast.execution.cluster_execution import cli as cli_module

    # `setup` is a click Command; the body lives on its callback.
    source = inspect.getsource(cli_module.setup.callback)
    assert "vast exec cluster token" in source
    assert "base64 -d" not in source, (
        "setup should not hand out a kubectl incantation now that a command exists")
