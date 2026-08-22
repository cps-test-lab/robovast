# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""How a campaign archive is named on a share, and how that name is read back.

The variant is in the name because nothing else records it: there is no manifest beside
the object, so a name that did not say it would leave "raw or postprocessed?" answerable
only by downloading and looking. An archive written before the variant was part of the
name has no token and is read as raw -- which is what it is, since the only thing that
ever wrote one was the campaign-end upload, and that runs before postprocessing.
"""

import pytest

from robovast.execution.share_providers.naming import (POSTPROCESSED, RAW, archive_name,
                                                       campaign_variant, parse_archive_name)

CAMPAIGN = "nav-2026-08-18-194018"


@pytest.mark.parametrize("variant", [RAW, POSTPROCESSED])
def test_a_name_round_trips_through_the_parser(variant):
    assert parse_archive_name(archive_name(CAMPAIGN, variant)) == (CAMPAIGN, variant)


def test_an_unsuffixed_legacy_archive_reads_as_raw():
    # Not a compatibility branch that has to be remembered: it is the same rule, applied
    # to a name whose token is absent. Archives already on a share must stay listable.
    assert parse_archive_name(f"{CAMPAIGN}.tar.gz") == (CAMPAIGN, RAW)


def test_a_provider_prefix_is_not_part_of_the_campaign_id():
    # GCS prefixes keys; the caller strips the directory part before parsing, and the
    # parser must not be fooled into accepting one.
    assert parse_archive_name(f"results/{CAMPAIGN}.raw.tar.gz") is None


@pytest.mark.parametrize("name", [
    "notacampaign.tar.gz",            # no timestamp -> not a campaign id
    f"{CAMPAIGN}.raw.tgz",            # not the extension we write
    f"{CAMPAIGN}.raw",                # no extension at all
    "some-unrelated-file.tar.gz",
])
def test_names_that_are_not_campaign_archives_are_refused(name):
    # A listing that accepted these would offer to import whatever else lives on the
    # share -- shares are shared, and not everything on one is ours.
    assert parse_archive_name(name) is None


def test_an_unknown_variant_cannot_be_written():
    with pytest.raises(ValueError, match="unknown archive variant"):
        archive_name(CAMPAIGN, "processed-ish")


def test_the_variant_is_read_off_the_campaign_not_passed_in(tmp_path):
    # data.db is postprocessing's output and nothing else writes it, so the campaign-end
    # upload and a later `vast share export` reach the same verdict without being told.
    campaign = tmp_path / CAMPAIGN
    (campaign / "_execution").mkdir(parents=True)
    assert campaign_variant(campaign) == RAW
    (campaign / "_execution" / "data.db").write_bytes(b"")
    assert campaign_variant(campaign) == POSTPROCESSED
