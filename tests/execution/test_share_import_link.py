# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast share import`` reads the link the web UI's import dialog hands out.

The dialog offers a copy button per campaign so that "here, take this one" is something
you can paste to a colleague. Half of the people it reaches paste into a terminal, so the
CLI reads it too -- otherwise the button produces something that works in exactly one of
the two places a campaign can be imported.

This is the only place the link's spelling exists in Python, and it is parse-only: the UI
mints these, nothing here does. That is what stops it drifting into a second, disagreeing
definition of what the link looks like -- the shared one is the grammar block in
``docs/developer_guide.rst``.

No host appears in these cases, and none needs to: only the fragment is read, which is
what makes a full URL, a sub-path deployment and a bare fragment the same input.
"""

import pytest

from robovast.execution.share_cli import campaign_from_ui_link

CAMPAIGN = "nav-2026-08-18-194018"


@pytest.mark.parametrize("text", [
    f"https://example.com/#/execution?import={CAMPAIGN}",
    # Served under a sub-path: the fragment is untouched by it, which is the point of
    # reading the fragment rather than the path.
    f"https://example.com/robovast/ui/#/execution?import={CAMPAIGN}",
    # Pasted without the host, which is what a copy out of an address bar can look like.
    f"#/execution?import={CAMPAIGN}",
    # A trailing slash on the view is still that view.
    f"#/execution/?import={CAMPAIGN}",
])
def test_a_link_names_its_campaign(text):
    assert campaign_from_ui_link(text) == CAMPAIGN


def test_the_value_is_url_decoded():
    # The UI encodes the search string, so anything that had to be escaped has to come
    # back as written or the resolver looks for a campaign nobody named.
    assert campaign_from_ui_link("#/execution?import=my%20campaign") == "my campaign"


@pytest.mark.parametrize("text", [
    CAMPAIGN,                                     # a plain campaign id
    f"{CAMPAIGN}.raw.tar.gz",                     # a full archive name
    f"https://example.com/#/results/explorer/{CAMPAIGN}",   # a link to another view
    "#/execution",                                # the campaign view, naming nothing
    "#/execution?import=",                        # ...and an empty request in it
    "#/execution?batch=3",                        # some other query
    "https://example.com/downloads/x.tar.gz",     # not a UI link at all
    "",
])
def test_anything_else_is_not_a_link(text):
    # None rather than a guess: the argument is then used verbatim, so an unrecognised
    # paste fails naming what the user actually typed instead of something invented here.
    assert campaign_from_ui_link(text) is None
