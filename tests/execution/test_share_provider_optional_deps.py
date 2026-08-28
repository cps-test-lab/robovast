# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A provider whose optional dependency is absent must stay silent until it is used.

``load_share_provider_plugins`` loads *every* registered provider, on every share
operation and on every campaign import. An SFTP provider importing paramiko at module
scope makes an install without the ``sftp`` extra warn about it several times per
command -- to people using a WebDAV or GCS share who will never open an SFTP connection.
"""

import importlib
import logging
import sys

import click
import pytest

from robovast.execution.share_providers import (load_share_provider_plugins,
                                                unavailable_share_type_message)


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


def test_sftp_entry_point_loads_without_paramiko(no_paramiko, caplog):
    """The regression: loading the plugin set must not warn about a missing paramiko."""
    with caplog.at_level(logging.WARNING):
        providers = load_share_provider_plugins()
    assert "sftp" in providers
    assert providers["sftp"] is no_paramiko.SftpShareProvider
    assert "Failed to load share provider plugin" not in caplog.text
    assert "paramiko" not in caplog.text


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


def test_a_provider_that_does_fail_to_load_is_not_reported_as_unknown(monkeypatch):
    """A share type that is spelled right must not be reported as unknown.

    Driven through a provider that really does break -- WebDAV needs ``requests`` at
    module scope -- rather than by planting the failure, so what is checked is the path
    a user takes: the loader records why, and the lookup that then misses says so.
    """
    monkeypatch.setitem(sys.modules, "requests", None)
    monkeypatch.delitem(sys.modules, "robovast.execution.share_providers.webdav",
                        raising=False)
    providers = load_share_provider_plugins()
    assert "webdav" not in providers

    message = unavailable_share_type_message("webdav", providers)
    assert "requests" in message
    assert "Unknown share type" not in message

    # A name nobody registered still gets the list of what is there.
    unknown = unavailable_share_type_message("wbdav", providers)
    assert "Unknown share type 'wbdav'" in unknown
    assert "gcs" in unknown
