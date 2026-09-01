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

from robovast.execution.share_providers.naming import (POSTPROCESSED, POSTPROCESSING_RECORD,
                                                       RAW, archive_name, campaign_variant,
                                                       parse_archive_name)

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


def _record(entries):
    """Postprocessing's provenance record as it writes it, with *entries* many entries."""
    import yaml
    return yaml.dump({"generated_by": "robovast",
                      "entries": [{"output": f"run-{i}/nav.csv", "sources": ["run.bag"],
                                   "plugin": "rosbags_to_csv", "params": {}}
                                  for i in range(entries)]})


def test_the_variant_is_read_off_the_campaign_not_passed_in(tmp_path):
    # Postprocessing's provenance record is what tells the two apart, and it is read off
    # the directory -- so the campaign-end upload and a later `vast share export` reach
    # the same verdict without being told which they are looking at.
    campaign = tmp_path / CAMPAIGN
    (campaign / "_transient").mkdir(parents=True)
    assert campaign_variant(campaign) == RAW
    (campaign / POSTPROCESSING_RECORD).write_text(_record(2), encoding="utf-8")
    assert campaign_variant(campaign) == POSTPROCESSED


def test_a_record_with_no_entries_is_not_postprocessed(tmp_path):
    """The record is written even when every step failed or none was configured.

    Reading its mere existence as "postprocessed" would put a campaign with no derived
    data on a share under a name promising results -- the error a recipient cannot
    detect without unpacking the archive and going looking, which is the whole thing
    the variant in the name exists to spare them.
    """
    campaign = tmp_path / CAMPAIGN
    (campaign / "_transient").mkdir(parents=True)
    (campaign / POSTPROCESSING_RECORD).write_text(_record(0), encoding="utf-8")
    assert campaign_variant(campaign) == RAW


def test_a_record_that_is_not_a_provenance_record_is_refused(tmp_path):
    # Postprocessing's own output, in a format it wrote: a broken one is a defect, and
    # both answers available here would be inventions.
    campaign = tmp_path / CAMPAIGN
    (campaign / "_transient").mkdir(parents=True)
    (campaign / POSTPROCESSING_RECORD).write_text("- not\n- a mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="provenance record"):
        campaign_variant(campaign)


def _tar(tmp_path, name, members):
    """A tar holding *members* (campaign-relative path -> contents) under one top dir."""
    import tarfile
    root = tmp_path / name
    for rel, text in members.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    out = tmp_path / f"{name}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(root, arcname=name)
    return out


def test_an_archives_variant_comes_from_the_record_inside_it(tmp_path):
    """The uploader reads the archive rather than the name it happens to have on disk.

    An archive is analysed offline by whoever receives it, so the variant has to be
    decidable from its own members -- never from the results index, which the recipient
    cannot reach.
    """
    import tarfile

    from robovast.execution.share_cli import _read_archive_identity

    base = {"_config/nav.vast": "x", "_execution/controller.log": "x"}
    raw = _tar(tmp_path, CAMPAIGN, base)
    assert _read_archive_identity(tarfile, str(raw)) == (CAMPAIGN, RAW)

    done = _tar(tmp_path, "nav-2026-08-19-101500",
                {**base, POSTPROCESSING_RECORD: _record(3)})
    assert _read_archive_identity(tarfile, str(done)) == ("nav-2026-08-19-101500",
                                                          POSTPROCESSED)

    empty = _tar(tmp_path, "nav-2026-08-19-102500",
                 {**base, POSTPROCESSING_RECORD: _record(0)})
    assert _read_archive_identity(tarfile, str(empty)) == ("nav-2026-08-19-102500", RAW)


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

    # A campaign that died before its config was frozen: logs and nothing to re-run.
    bad = _tar(tmp_path, "died-2026-08-20-110402", {"_execution/controller.log": "x"})
    with pytest.raises(click.ClickException, match="_config/"):
        _read_archive_identity(tarfile, str(bad))


def test_a_push_refuses_an_archive_whose_record_is_broken(tmp_path):
    import tarfile

    import click

    from robovast.execution.share_cli import _read_archive_identity

    broken = _tar(tmp_path, "nav-2026-08-20-120000",
                  {"_config/nav.vast": "x", POSTPROCESSING_RECORD: "[unclosed\n"})
    with pytest.raises(click.ClickException, match="not readable as YAML"):
        _read_archive_identity(tarfile, str(broken))
