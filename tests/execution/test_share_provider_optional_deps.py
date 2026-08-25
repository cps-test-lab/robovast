# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A provider whose optional dependency is absent must stay silent until it is used.

``load_share_provider_plugins`` loads *every* registered provider, on every share
operation and on every campaign import. The SFTP provider used to import paramiko at
module scope, so an install without the ``sftp`` extra warned about it several times per
command -- to people using a WebDAV or GCS share who will never open an SFTP connection.
"""

import importlib
import sys

import click
import pytest


@pytest.fixture
def no_paramiko(monkeypatch):
    """Make ``import paramiko`` fail, with the sftp module re-imported under that.

    ``None`` in ``sys.modules`` is how the import system is told a module is absent, so
    this reproduces an install without the extra even though the dev venv has it.
    Dropping the already-imported sftp module is what makes the entry point reload it.
    """
    monkeypatch.setitem(sys.modules, "paramiko", None)
    monkeypatch.delitem(sys.modules, "robovast.execution.share_providers.sftp",
                        raising=False)
    # monkeypatch restores both sys.modules entries afterwards.
    return importlib.import_module("robovast.execution.share_providers.sftp")


def test_sftp_entry_point_loads_without_paramiko(no_paramiko):
    """The regression: loading the plugin set must not warn about a missing paramiko."""
    from robovast.execution import share_providers
    providers = share_providers.load_share_provider_plugins()
    assert "sftp" in providers
    assert providers["sftp"] is no_paramiko.SftpShareProvider
    assert share_providers._LOAD_ERRORS == {}


def test_using_sftp_without_paramiko_names_the_extra(no_paramiko, monkeypatch):
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "sftp")
    monkeypatch.setenv("ROBOVAST_SFTP_HOST", "sftp.example.com")
    monkeypatch.setenv("ROBOVAST_SFTP_USER", "u")
    monkeypatch.setenv("ROBOVAST_SFTP_PASSWORD", "p")
    monkeypatch.setenv("ROBOVAST_SFTP_REMOTE_DIR", "/srv/archives")
    provider = no_paramiko.SftpShareProvider()
    with pytest.raises(click.UsageError) as excinfo:
        provider.verify_access()
    assert "paramiko" in str(excinfo.value)
    assert "robovast[sftp]" in str(excinfo.value)


def test_broken_provider_is_not_reported_as_unknown(monkeypatch):
    """A registered-but-unloadable share type must not be reported as a typo."""
    from robovast.execution import share_providers
    monkeypatch.setitem(share_providers._LOAD_ERRORS, "sftp",
                        "No module named 'paramiko'")
    message = share_providers.unavailable_share_type_message("sftp", {"webdav": object})
    assert "paramiko" in message
    assert "Unknown share type" not in message

    unknown = share_providers.unavailable_share_type_message("sfpt", {"webdav": object})
    assert "Unknown share type 'sfpt'" in unknown
    assert "webdav" in unknown
