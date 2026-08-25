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


def _tar(tmp_path, name, members):
    """A tar holding *members* (campaign-relative paths) under one top-level dir."""
    import tarfile
    root = tmp_path / name
    for rel in members:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    out = tmp_path / f"{name}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(root, arcname=name)
    return out


def test_a_push_refuses_an_archive_that_could_never_be_imported(tmp_path):
    """``share push`` is the remaining way a bad archive reaches a share.

    Both service-side exports refuse a campaign with no frozen ``_config/``, but a push
    uploads a file somebody built earlier and never looked inside beyond its top-level
    name. Such an archive uploads, lists and downloads exactly like a good one and fails
    only at the far end, on somebody else's service, where the source is out of reach --
    and the identity read already walks every member, so refusing costs nothing.
    """
    import tarfile

    import click

    from robovast.execution.share_cli import _read_archive_identity

    good = _tar(tmp_path, CAMPAIGN, ["_config/nav.vast", "_execution/controller.log"])
    assert _read_archive_identity(tarfile, str(good)) == (CAMPAIGN, RAW)

    # A campaign that died before its config was frozen: logs and nothing to re-run.
    bad = _tar(tmp_path, "died-2026-08-20-110402", ["_execution/controller.log"])
    with pytest.raises(click.ClickException, match="_config/"):
        _read_archive_identity(tarfile, str(bad))
