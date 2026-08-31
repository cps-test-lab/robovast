# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Whether a published dataset can be reproduced, and by whom.

The distinction these tests defend is ``private`` vs ``opaque``. Refusing every input a stranger
cannot fetch would refuse most real research -- private asset libraries and internal registries
are normal. Refusing only what nobody can *identify* is a claim that can actually be honoured:
a private input with a recorded commit is reproducible by someone with access, while a floating
ref or a local-only image id is reproducible by no one, including us.

Publication is where this is checked because it is the last cheap moment. Afterwards the dataset
has a DOI, is cited, and every gap in it is permanent.
"""

import json
from pathlib import Path

import pytest
import yaml

from robovast.results_processing.reproducibility import (OPAQUE, PRIVATE, PUBLIC,
                                                        classify_campaign_inputs,
                                                        reproducibility_manifest)


def _campaign(tmp_path, execution: dict, *, plugins=None, providers=None) -> Path:
    root = tmp_path / "c-2026-01-01-000000"
    (root / "_execution").mkdir(parents=True)
    (root / "_execution" / "execution.yaml").write_text(yaml.safe_dump(execution),
                                                        encoding="utf-8")
    if plugins is not None:
        (root / "_execution" / "plugins.yaml").write_text(yaml.safe_dump(plugins),
                                                          encoding="utf-8")
    if providers is not None:
        (root / "_execution" / "providers.yaml").write_text(yaml.safe_dump(providers),
                                                            encoding="utf-8")
    return root


def _by_input(manifest: dict) -> dict:
    return {entry["input"]: entry for entry in manifest["inputs"]}


_CLEAN = {
    "robovast_revision": "a" * 40,
    "robovast_dirty": False,
    "images": {"scenario": "ghcr.io/org/img:1"},
    "image_revisions": {"scenario": "ghcr.io/org/img@sha256:" + "b" * 64},
}


def test_a_fully_identified_campaign_is_publishable(tmp_path):
    manifest = reproducibility_manifest(_campaign(
        tmp_path, _CLEAN,
        plugins={"p": {"version": "1.0"}},
        providers={"assets": {"version": "0.1.0", "commit": "c" * 40}}))
    assert manifest["publishable"] is True
    assert manifest["opaque"] == []


def test_a_private_input_with_a_commit_does_not_block(tmp_path):
    """The whole design. A private asset library is normal, and its commit is what makes it
    reproducible by someone with access -- so it is named, not refused."""
    manifest = reproducibility_manifest(_campaign(
        tmp_path, _CLEAN,
        plugins={"p": {"version": "1.0"}},
        providers={"roqsim_assets": {"version": "0.1.0", "commit": "d" * 40,
                                     "url": "file:///opt/private-assets"}}))
    entry = _by_input(manifest)["provider[roqsim_assets]"]
    assert entry["class"] == PRIVATE
    assert manifest["publishable"] is True, "private must not block; only unidentifiable does"


def test_a_dirty_checkout_is_opaque(tmp_path):
    """The recorded revision does not describe the code that produced the results, so nobody --
    including us -- can reconstruct it. That is exactly what must not be published silently."""
    execution = dict(_CLEAN, robovast_dirty=True)
    manifest = reproducibility_manifest(_campaign(
        tmp_path, execution, plugins={}, providers={}))
    entry = _by_input(manifest)["robovast"]
    assert entry["class"] == OPAQUE
    assert "DIRTY" in entry["why"]
    assert manifest["publishable"] is False


def test_a_package_version_is_not_an_identity(tmp_path):
    """robovast_version falls back to the installed semver, so `2.0.0` names no commit -- and the
    message says where a real one might still be derived from."""
    execution = {k: v for k, v in _CLEAN.items() if k != "robovast_revision"}
    execution["robovast_version"] = "2.0.0"
    entry = _by_input(reproducibility_manifest(
        _campaign(tmp_path, execution, plugins={}, providers={})))["robovast"]
    assert entry["class"] == OPAQUE
    assert "backfill-provenance" in entry["why"]


def test_a_local_image_id_is_opaque(tmp_path):
    """Only the machine that ran it ever had those bytes, so the dataset depends on something
    that cannot be obtained anywhere."""
    execution = dict(_CLEAN, image_revisions={"scenario": "sha256:" + "e" * 64})
    entry = _by_input(reproducibility_manifest(
        _campaign(tmp_path, execution, plugins={}, providers={})))["image[scenario]"]
    assert entry["class"] == OPAQUE
    assert "cannot be pulled" in entry["why"]


def test_a_digest_in_either_field_counts(tmp_path):
    """The two lanes fill `images` and `image_revisions` differently, so reading only one
    reported a campaign whose digest was in the other as having none -- while quoting that very
    digest back in the message."""
    execution = {"robovast_revision": "a" * 40, "robovast_dirty": False,
                 "images": {"sut": "ghcr.io/org/img@sha256:" + "f" * 64},
                 "image_revisions": {}}
    entry = _by_input(reproducibility_manifest(
        _campaign(tmp_path, execution, plugins={}, providers={})))["image[sut]"]
    assert entry["class"] in (PUBLIC, PRIVATE)
    assert "digest" in entry["why"]


def test_a_plugin_resolved_nowhere_is_opaque(tmp_path):
    manifest = reproducibility_manifest(_campaign(
        tmp_path, _CLEAN, plugins={"ghost": {"requested": "ghost", "resolved": False}},
        providers={}))
    entry = _by_input(manifest)["plugin[ghost]"]
    assert entry["class"] == OPAQUE
    assert manifest["publishable"] is False


@pytest.mark.parametrize("missing", ["plugins", "providers"])
def test_an_unrecorded_category_is_opaque_not_assumed_empty(tmp_path, missing):
    """Absent means unknown. A campaign from before these were captured may well have declared
    plugins, and the dataset cannot show otherwise -- so it must not read as "there were none"."""
    kwargs = {"plugins": {}, "providers": {}}
    kwargs[missing] = None
    manifest = reproducibility_manifest(_campaign(tmp_path, _CLEAN, **kwargs))
    assert _by_input(manifest)[missing]["class"] == OPAQUE


def test_a_campaign_with_no_execution_record_is_opaque(tmp_path):
    root = tmp_path / "c-2026-01-01-000000"
    root.mkdir()
    manifest = reproducibility_manifest(root)
    assert manifest["publishable"] is False
    assert manifest["counts"][OPAQUE] == 1


def test_looks_public_is_a_short_list_not_a_guess():
    """A wrong `public` overstates the dataset; a needless `private` only understates it. So the
    host list is deliberately conservative rather than "anything URL-shaped"."""
    from robovast.results_processing.reproducibility import _looks_public

    assert _looks_public("https://github.com/org/repo")
    assert not _looks_public("https://git.internal.example/org/repo")
    assert not _looks_public("")


# ---------------------------------------------------------------------------
# The gate: what it writes, where, and what it refuses
# ---------------------------------------------------------------------------

def _gate(campaign_root, *, allow_opaque=False):
    """Run the publication gate, returning ``(ok, message, printed_lines)``."""
    from robovast.results_processing.publication import _reproducibility_gate

    printed: list = []
    ok, message = _reproducibility_gate(campaign_root, printed.append, allow_opaque)
    return ok, message, printed


def test_the_manifest_is_grouped_with_the_other_publication_artifacts(tmp_path):
    """The `metadata.` prefix keeps the three publication-time artifacts together.

    `metadata.yaml` and `metadata.prov.json` are written at the same stage and at the same level,
    while `campaign.db` and the `_`-prefixed directories belong to other stages. A bare
    `reproducibility.yaml` would sort away from its siblings for no reason, so the name is pinned
    here rather than left to be tidied.
    """
    from robovast.results_processing.publication import REPRODUCIBILITY_FILENAME

    assert REPRODUCIBILITY_FILENAME == "metadata.reproducibility.yaml"

    root = _campaign(tmp_path, _CLEAN, plugins={}, providers={})
    ok, _, _ = _gate(root)
    assert ok
    written = root / REPRODUCIBILITY_FILENAME
    assert written.exists(), sorted(p.name for p in root.iterdir())
    assert yaml.safe_load(written.read_text(encoding="utf-8"))["counts"]["opaque"] == 0


def test_an_opaque_input_is_refused_and_the_offender_named(tmp_path):
    """Refusing without saying what to fix would only move the discovery later."""
    execution = dict(_CLEAN, image_revisions={"scenario": "sha256:" + "e" * 64})
    root = _campaign(tmp_path, execution, plugins={}, providers={})
    ok, message, printed = _gate(root)
    assert not ok
    assert "image[scenario]" in " ".join(printed)
    assert "metadata.reproducibility.yaml" in message


def test_a_granted_exemption_is_recorded_not_merely_allowed(tmp_path):
    """The whole point of the file: a reader can check the claim instead of trusting it.

    An exemption that only changed the exit status would leave a published dataset saying
    nothing about the fact that somebody knowingly published an unidentifiable input.
    """
    execution = dict(_CLEAN, image_revisions={"scenario": "sha256:" + "e" * 64})
    root = _campaign(tmp_path, execution, plugins={}, providers={})
    ok, _, _ = _gate(root, allow_opaque=True)
    assert ok
    manifest = yaml.safe_load(
        (root / "metadata.reproducibility.yaml").read_text(encoding="utf-8"))
    assert manifest["exemption_granted"] is True
    assert manifest["counts"]["opaque"] == 1


def test_no_exemption_is_recorded_when_there_was_nothing_to_exempt(tmp_path):
    """`--allow-opaque` on a clean campaign must not stamp it as exempted."""
    root = _campaign(tmp_path, _CLEAN, plugins={}, providers={})
    _gate(root, allow_opaque=True)
    manifest = yaml.safe_load(
        (root / "metadata.reproducibility.yaml").read_text(encoding="utf-8"))
    assert manifest["exemption_granted"] is False


def test_a_campaign_that_declared_no_plugins_is_publishable(tmp_path):
    """The regression that made this whole gate unusable.

    Three places disagreed: the writer skipped writing an empty record because "absence already
    means no plugins", the reader documented absence as *unknown*, and this classifier treats
    unknown as opaque. So a campaign with no plugins -- most of them, `camera_smoke` included --
    was refused publication with nothing its author could do about it. An empty record is now
    written and read as `{}`, which contributes no input at all.
    """
    from robovast.common.campaign_data import write_plugins_record, write_providers_record

    root = _campaign(tmp_path, _CLEAN)
    write_plugins_record(root, {})
    write_providers_record(root, {})

    manifest = reproducibility_manifest(root)
    assert manifest["publishable"] is True
    assert manifest["opaque"] == []
    assert not [entry for entry in manifest["inputs"]
                if entry["input"] in ("plugins", "providers")]


def test_a_campaign_predating_the_records_is_still_opaque(tmp_path):
    """The other half: fixing empty must not make absence look answered."""
    manifest = reproducibility_manifest(_campaign(tmp_path, _CLEAN))
    assert manifest["publishable"] is False
    assert sorted(manifest["opaque"]) == ["plugins", "providers"]


# -- the recipe: reproducible after the digest is gone ------------------------------

_RECIPE = {
    "base_image": "ros:jazzy-ros-base@sha256:" + "1" * 64,
    "ubuntu_snapshot": "20260819T003043Z",
    "ros_snapshot": "2026-06-18",
    "source": "https://github.com/cps-test-lab/robovast",
}


def _with_lock(root, role="sut"):
    lock = root / "_execution" / "build_manifest"
    lock.mkdir(parents=True, exist_ok=True)
    (lock / f"{role}.json").write_text(json.dumps({"apt": {"tree": "2.1.1-2"}, "pip": {}}))
    return root


def _image_class(root, role="sut"):
    return next(e["class"] for e in classify_campaign_inputs(root)
                if e["input"] == f"image[{role}]")


def test_a_complete_recipe_and_lock_is_reproducible_without_a_digest(tmp_path):
    """The case this gate used to refuse, and the reason the recipe is recorded at all.

    A digest is reproducible for exactly as long as the registry keeps the manifest. After that
    the recipe is what still answers -- the base it was built from, the dated archives it
    installed from, and the resolved versions beside them. Reading only the digest fields called
    such a campaign opaque and refused to publish it, which is the opposite of the truth.
    """
    root = _with_lock(_campaign(tmp_path, {
        "images": {"sut": "reg.example/sut:latest"},
        "image_build_refs": {"sut": dict(_RECIPE)},
    }))
    assert _image_class(root) == "public"


def test_a_recipe_without_its_lock_is_still_opaque(tmp_path):
    """Where to start is not which versions.

    Without the lock a rebuild re-resolves the author's loose specs and installs whatever is
    current -- a different experiment wearing the same name. That is not an identity, and
    saying so is the whole point of recording the lock separately.
    """
    root = _campaign(tmp_path, {
        "images": {"sut": "reg.example/sut:latest"},
        "image_build_refs": {"sut": dict(_RECIPE)},
    })
    assert _image_class(root) == "opaque"


def test_a_partial_recipe_does_not_count(tmp_path):
    """Two pins of three is not a partial answer -- it is a rebuild that installs today's."""
    partial = {k: v for k, v in _RECIPE.items() if k != "ubuntu_snapshot"}
    root = _with_lock(_campaign(tmp_path, {
        "images": {"sut": "reg.example/sut:latest"},
        "image_build_refs": {"sut": partial},
    }))
    assert _image_class(root) == "opaque"


def test_a_recipe_naming_sources_nobody_can_reach_is_private_not_public(tmp_path):
    """Rebuildable is not the same as rebuildable *by you*."""
    private = dict(_RECIPE, source="https://git.example.internal/theirs")
    root = _with_lock(_campaign(tmp_path, {
        "images": {"sut": "reg.example/sut:latest"},
        "image_build_refs": {"sut": private},
    }))
    assert _image_class(root) == "private"


def test_a_digest_still_wins_over_a_recipe(tmp_path):
    """Unchanged, and the order matters: the digest names the bytes that actually ran."""
    digest = "ghcr.io/cps-test-lab/robovast@sha256:" + "9" * 64
    root = _with_lock(_campaign(tmp_path, {
        "images": {"sut": digest},
        "image_build_refs": {"sut": dict(_RECIPE)},
    }))
    entry = next(e for e in classify_campaign_inputs(root) if e["input"] == "image[sut]")
    assert entry["class"] == "public"
    assert entry.get("digest") == digest
