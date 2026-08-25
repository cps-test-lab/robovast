# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The share listing comes back newest campaign first, and comes back the same twice.

Ordering is a contract here rather than a nicety: the web UI's import dialog renders the
listing in the order it arrives and does not re-sort, precisely so this rule lives in one
place -- the campaign-id timestamp parser is Python's, and a second copy of it in
TypeScript would be a second thing to keep right.

The sort cannot key on a modification time because no provider reports one: every
``list_campaign_archives_with_size`` yields ``(name, size)``. What it keys on instead is
the timestamp inside the campaign id, which is when the campaign *ran* -- the fact a
reader of this listing is actually after.
"""

import pytest

from robovast.service.interface import ShareListing
from robovast.service.local_transport import LocalTransport

#: Deliberately not in date order, and deliberately not in name order either -- sorting on
#: the id alone (what this used to do) puts `alpha` first, which is the oldest.
CAMPAIGNS = (
    "nav-2026-08-18-194018",
    "alpha-2025-01-02-030405",
    "obst-2026-08-22-084411",
    # The `<name>-YYYY-MM-DD-HHMMSScc` form, whose extra hundredths must not read as a
    # different or unparseable timestamp.
    "zeta-2026-08-22-08441199",
)

NEWEST_FIRST = (
    "zeta-2026-08-22-08441199",
    "obst-2026-08-22-084411",
    "nav-2026-08-18-194018",
    "alpha-2025-01-02-030405",
)


class _StubProvider:
    """A share that holds *objects*, in the order the provider happened to list them."""

    SHARE_TYPE = "stub"

    def __init__(self, objects):
        self._objects = list(objects)

    def list_campaign_archives_with_size(self):
        return [(name, 1) for name in self._objects]

    @staticmethod
    def archive_url(object_name):
        # None is a real answer, not a stub's shrug: sftp never has an openable link, and a
        # webdav one often needs credentials the reader lacks. Nothing here reads it.
        del object_name


@pytest.fixture(name="listing")
def _listing(monkeypatch):
    """Call ``list_share_archives`` against a stub share holding *objects*."""
    monkeypatch.setenv("ROBOVAST_SHARE_TYPE", "stub")
    transport = LocalTransport()

    def call(objects) -> ShareListing:
        monkeypatch.setattr(type(transport), "_share_provider",
                            lambda self: _StubProvider(objects))
        return transport.list_share_archives()

    return call


def test_newest_campaign_first(listing):
    result = listing(f"{c}.raw.tar.gz" for c in CAMPAIGNS)
    assert [a.campaign_id for a in result.archives] == list(NEWEST_FIRST)


def test_both_variants_of_one_campaign_are_ordered_between_themselves(listing):
    # They share an id *and* a timestamp, so the first two sort keys tie. Without the
    # object name as a third, their order would be whatever the provider listed -- and two
    # calls against a share that listed differently would disagree about which came first.
    campaign = "nav-2026-08-18-194018"
    forwards = listing([f"{campaign}.raw.tar.gz", f"{campaign}.postprocessed.tar.gz"])
    backwards = listing([f"{campaign}.postprocessed.tar.gz", f"{campaign}.raw.tar.gz"])
    assert ([a.object_name for a in forwards.archives]
            == [a.object_name for a in backwards.archives])


def test_a_name_that_is_not_a_campaign_archive_is_left_out(listing):
    # Not an ordering rule, but it is what lets the sort assume every id parses: an
    # unparseable name never reaches the key function.
    result = listing(["notes.txt", "nav-2026-08-18-194018.raw.tar.gz", "backup.tar.gz"])
    assert [a.campaign_id for a in result.archives] == ["nav-2026-08-18-194018"]


def test_no_share_configured_is_not_an_empty_share(listing, monkeypatch):
    monkeypatch.delenv("ROBOVAST_SHARE_TYPE", raising=False)
    result = listing([])
    assert result.configured is False
