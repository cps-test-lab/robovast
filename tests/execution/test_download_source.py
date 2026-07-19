# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``resolve_download_source`` picks postprocessed (service) vs raw (share).

Auto prefers a reachable service (postprocessed); an explicit variant forces its
source and errors when unreachable; neither source is an actionable error.
"""

import click
import pytest

from robovast.results_processing import cli as rcli


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Default: no service, no share. Individual tests opt in.
    monkeypatch.setattr(rcli, "_share_configured", lambda: False)
    import robovast.common.cli.service_target as st
    monkeypatch.setattr(st, "detected_service_url", lambda: None)
    yield


def _set(monkeypatch, service, share):
    import robovast.common.cli.service_target as st
    monkeypatch.setattr(st, "detected_service_url", lambda: "http://svc" if service else None)
    monkeypatch.setattr(rcli, "_share_configured", lambda: share)


def test_auto_prefers_postprocessed_when_service_reachable(monkeypatch):
    _set(monkeypatch, service=True, share=True)
    assert rcli.resolve_download_source("auto") == "postprocessed"


def test_auto_falls_back_to_raw_without_service(monkeypatch):
    _set(monkeypatch, service=False, share=True)
    assert rcli.resolve_download_source("auto") == "raw"


def test_auto_errors_when_nothing_reachable(monkeypatch):
    _set(monkeypatch, service=False, share=False)
    with pytest.raises(click.UsageError):
        rcli.resolve_download_source("auto")


def test_explicit_postprocessed_requires_service(monkeypatch):
    _set(monkeypatch, service=False, share=True)
    with pytest.raises(click.UsageError):
        rcli.resolve_download_source("postprocessed")


def test_explicit_raw_requires_share(monkeypatch):
    _set(monkeypatch, service=True, share=False)
    with pytest.raises(click.UsageError):
        rcli.resolve_download_source("raw")
