# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""An author may bring their own image, but not one nobody could later identify.

That single field otherwise makes a whole campaign untraceable: nothing in the results can say
what the image was, and the gap surfaces a year later when it is too late to ask. So a
user-supplied image must carry a ``provenance:`` block, and a campaign without one is refused
while it is still cheap to fix.

The boundary these tests defend is the one that makes the rule safe: it applies to **authoring**,
never to reading or retriggering an archived campaign. A recorded campaign's image digest already
is provenance for "these bytes ran", so refusing it would make exactly the old campaigns this
must keep re-runnable un-re-runnable.
"""

import pytest
import yaml

from robovast.common.execution import (IMAGE_TIER_BUILT, IMAGE_TIER_DECLARED, IMAGE_TIER_FAMILY,
                                       IMAGE_TIER_OPAQUE, image_provenance_tier,
                                       opaque_image_containers)

_PROVENANCE = {"source": "https://github.com/org/images", "revision": "a" * 40}


@pytest.mark.parametrize("block,tier", [
    # robovast builds it, so the base digest and every resolved package are recorded -- even
    # when an image is also named, because that image is then just the base.
    ({"system_packages": ["tree"]}, IMAGE_TIER_BUILT),
    ({"image": "ros:jazzy", "system_packages": ["tree"]}, IMAGE_TIER_BUILT),
    ({"python_packages": ["numpy"]}, IMAGE_TIER_BUILT),
    # ours: a family member, a build ref, or no image at all (the role/backend supplies one).
    ({"image": "family:robovast"}, IMAGE_TIER_FAMILY),
    ({"image": "build:proj"}, IMAGE_TIER_FAMILY),
    ({"backend": "roqsim"}, IMAGE_TIER_FAMILY),
    ({}, IMAGE_TIER_FAMILY),
    # the author's own image, with and without the one thing robovast cannot derive.
    ({"image": "reg/thing:1", "provenance": _PROVENANCE}, IMAGE_TIER_DECLARED),
    ({"image": "reg/thing:1"}, IMAGE_TIER_OPAQUE),
    ({"image": "reg/thing@sha256:" + "b" * 64}, IMAGE_TIER_OPAQUE),
])
def test_tiers(block, tier):
    assert image_provenance_tier("c", block)[0] == tier


def test_a_digest_alone_is_not_provenance():
    """A digest identifies bytes, not where they came from. Nobody can rebuild or audit an image
    from a digest whose source is unrecorded, so it is still opaque -- which is the distinction
    that makes the rule about *traceability* rather than about pinning."""
    tier, why = image_provenance_tier("sut", {"image": "reg/thing@sha256:" + "c" * 64})
    assert tier == IMAGE_TIER_OPAQUE
    assert "provenance" in why


def test_the_refusal_names_both_ways_out():
    """A refusal an author cannot act on is worse than none. Both fixes have to be in it: declare
    the block, or let robovast build the image instead."""
    _tier, why = image_provenance_tier("sut", {"image": "reg/thing:1"})
    assert "provenance:" in why and "revision:" in why
    assert "system_packages" in why
    assert "execution.containers.sut" in why


def test_the_classification_never_inspects_the_image(monkeypatch):
    """Declarative on purpose. A check that read image labels would answer differently depending
    on whether the image happened to be pulled locally -- so the same .vast would validate on one
    machine and fail on another, in the collect-all validator the web editor and an agent hit."""
    import subprocess

    def boom(*_args, **_kwargs):
        raise AssertionError("classification must not run a subprocess")

    monkeypatch.setattr(subprocess, "run", boom)
    assert image_provenance_tier("sut", {"image": "reg/thing:1"})[0] == IMAGE_TIER_OPAQUE


def test_only_the_offending_containers_are_reported():
    execution = {"containers": {
        "scenario": {"image": "family:robovast"},
        "sut": {"image": "reg/a:1"},
        "sim": {"image": "reg/b:1", "provenance": _PROVENANCE},
        "helper": {"image": "reg/c:1"},
    }}
    assert [name for name, _why in opaque_image_containers(execution)] == ["helper", "sut"]


def test_a_config_with_no_containers_is_not_refused():
    """Nothing named is nothing to identify -- the backend or the role default supplies it."""
    assert opaque_image_containers({}) == []
    assert opaque_image_containers({"containers": {}}) == []


def test_the_validator_reports_it_as_a_problem_not_an_exception(tmp_path):
    """Collect-all: an author must see this beside everything else wrong with the file, not
    instead of it."""
    from robovast.common.config_validation import validate_project_file

    vast = tmp_path / "c.vast"
    vast.write_text(yaml.safe_dump({
        "version": 2, "metadata": {"name": "p"},
        "execution": {"containers": {"sut": {"image": "reg/thing:1"}},
                      "scenario_file": "s.osc", "runs": 1}}), encoding="utf-8")
    (tmp_path / "s.osc").write_text("scenario p:\n    do serial:\n        wait elapsed(1s)\n",
                                    encoding="utf-8")
    report = validate_project_file(str(vast))
    assert report["valid"] is False
    stages = {p["stage"] for p in report["problems"]}
    assert "image-provenance" in stages
    offending = next(p for p in report["problems"] if p["stage"] == "image-provenance")
    assert offending["config"] == "sut"
    assert offending["field"] == "execution.containers.sut.provenance"


def test_reading_an_archived_campaign_is_never_refused(tmp_path):
    """The boundary that keeps this rule from breaking the thing it sits next to.

    An archived campaign may well name an opaque image -- most historic ones do. Its recorded
    digest is provenance for "these bytes ran", which is the question a re-run actually asks, so
    loading it must succeed. Enforcing the rule in the model instead of on the authoring path
    would have made exactly these campaigns unreadable.
    """
    from robovast.common.common import load_config
    from robovast.common.config import validate_config

    raw = {"version": 2, "metadata": {"name": "old"},
           "execution": {"containers": {"sut": {"image": "harbor.internal/x@sha256:" + "d" * 64}},
                         "scenario_file": "s.osc", "runs": 1}}
    vast = tmp_path / "archived.vast"
    vast.write_text(yaml.safe_dump(raw), encoding="utf-8")

    validate_config(raw)                 # the model does not know about the rule
    assert load_config(str(vast))        # nor does loading
