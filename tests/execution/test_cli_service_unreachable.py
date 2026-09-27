# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A ``vast`` verb whose service does not answer says so, in a sentence that names the
address and where the address came from -- not a transport traceback.

The stored login is the usual way here: a service that was moved or taken down leaves
every command failing on its old URL, and from the socket that is indistinguishable from
a typo. Naming the source of the address is what sends the reader to the right fix."""

from click.testing import CliRunner

from robovast.client import service_target
from robovast.client.cli import cli


def test_the_failure_names_the_address_and_its_source(monkeypatch):
    monkeypatch.setattr(service_target, "detected_service_url", lambda: "http://127.0.0.1:1")

    result = CliRunner().invoke(cli, ["workspace", "list"])

    assert result.exit_code == 1
    assert "no robovast-service answered at http://127.0.0.1:1" in result.output
    assert "Connection refused" in result.output
    assert "'vast login'" in result.output
    assert "Traceback" not in result.output
    assert "HTTPConnectionPool" not in result.output
