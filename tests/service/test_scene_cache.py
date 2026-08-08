# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The on-demand scene cache: world identity, the hit decision, and what it refuses.

The properties here are the ones that would fail *invisibly* — a cache that never hits still renders the
right picture, just slowly; a key that ignores the overrides renders the wrong picture confidently.
"""

import json
import os
import shlex

import pytest

from robovast.service import scene_cache


def _set_values(command: str) -> list:
    """The value of every ``--set`` in *command*, as the generator's ``shlex.split`` sees them."""
    parts = shlex.split(command)
    return [parts[i + 1] for i, part in enumerate(parts) if part == "--set"]


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOVAST_SCENE_CACHE", str(tmp_path / "scenes"))
    scene_cache._locks.clear()
    yield


def _campaign(tmp_path, image="harbor/x@sha256:" + "a" * 64):
    root = tmp_path / "campaign-2026-08-06-000000"
    (root / "_execution").mkdir(parents=True)
    (root / "_execution" / "execution.yaml").write_text(
        f"image: build:x\nimage_revision: {image}\n", encoding="utf-8")
    return str(root)


def _manifest(**over):
    base = {"producer": "rst", "world": "pkg:depot", "overrides": {}}
    base.update(over)
    return base


def test_identity_comes_from_the_capture_and_the_pinned_image(tmp_path):
    ident = scene_cache.world_identity(_campaign(tmp_path), _manifest())
    assert ident["world"] == "pkg:depot"
    assert ident["image"].startswith("harbor/x@sha256:")
    assert ident["overrides_known"] is True


def test_a_run_that_does_not_name_its_world_is_refused(tmp_path):
    with pytest.raises(scene_cache.SceneUnavailable, match="does not name the world"):
        scene_cache.world_identity(_campaign(tmp_path), _manifest(world=None))


def test_a_mutable_tag_refuses_to_cache(tmp_path):
    """A tag can name different bytes tomorrow, so an entry keyed on it may be silently stale.

    The refusal now comes from ``campaign_role_image``, which reports every source it tried;
    matching on the tag itself keeps this pinned to the behaviour rather than the wording.
    """
    with pytest.raises(scene_cache.SceneUnavailable, match="harbor/x:latest"):
        scene_cache.world_identity(_campaign(tmp_path, image="harbor/x:latest"), _manifest())


def test_a_local_image_id_is_accepted(tmp_path):
    """The local lane records `docker inspect .Id`, which is immutable though not a registry digest."""
    ident = scene_cache.world_identity(
        _campaign(tmp_path, image="sha256:" + "b" * 64), _manifest())
    assert ident["image"].startswith("sha256:")


def test_missing_overrides_is_unknown_not_empty(tmp_path):
    """Absent must not be read as `{}` -- that compiles the *unoverridden* world for a run that varied it."""
    manifest = _manifest()
    del manifest["overrides"]
    ident = scene_cache.world_identity(_campaign(tmp_path), manifest)
    assert ident["overrides_known"] is False
    assert ident["overrides"] == {}, "we still have to compile something, but the caller must be told"


def test_the_key_is_the_world_and_not_the_campaign(tmp_path):
    """Two campaigns, same image and world -> one cache entry. This is the whole dedup claim."""
    a = scene_cache.world_identity(_campaign(tmp_path / "a"), _manifest())
    b = scene_cache.world_identity(_campaign(tmp_path / "b"), _manifest())
    assert scene_cache.cache_key(a) == scene_cache.cache_key(b)


@pytest.mark.parametrize("field,value", [
    ("world", "pkg:other"),
    ("overrides", {"plugins": {"floorplan": {"size": 4.0}}}),
])
def test_the_key_separates_worlds_and_overrides(tmp_path, field, value):
    base = scene_cache.world_identity(_campaign(tmp_path / "a"), _manifest())
    other = scene_cache.world_identity(_campaign(tmp_path / "b"), _manifest(**{field: value}))
    assert scene_cache.cache_key(base) != scene_cache.cache_key(other)


def test_the_key_separates_exporter_options(tmp_path):
    ident = scene_cache.world_identity(_campaign(tmp_path), _manifest())
    assert scene_cache.cache_key(ident, 1024) != scene_cache.cache_key(ident, 2048)


def test_overrides_become_dotlist_set_flags(tmp_path):
    ident = scene_cache.world_identity(
        _campaign(tmp_path),
        _manifest(overrides={"plugins": {"floorplan": {"size": 4.0}}, "sim": {"pacing": "asap"}}))
    cmd = scene_cache._command_for(ident, 1024)
    # Asserted after shlex.split, because that is what the generator does with this string -- a
    # substring match passes for a value that will be torn into several arguments.
    assert _set_values(cmd) == ["plugins.floorplan.size=4.0", 'sim.pacing="asap"']
    assert "--manifest {out}/.generated.json" in cmd, "the tool reports its own inputs"


def test_a_vector_override_stays_one_argument(tmp_path):
    """A campaign that sweeps a position records a LIST, and a list has spaces in it.

    Unquoted, `plugins.parcel.pos=[11.8, 4.55, 0.762]` reached the exporter as three arguments and it
    exited 2 -- reported to whoever opened the run view as "no 3D geometry", which reads like the
    campaign never recorded any rather than like a quoting bug here.
    """
    ident = scene_cache.world_identity(
        _campaign(tmp_path),
        _manifest(overrides={"plugins": {"parcel": {"pos": [11.8, 4.55, 0.762]}}}))
    assert _set_values(scene_cache._command_for(ident, 1024)) == ["plugins.parcel.pos=[11.8,4.55,0.762]"]


def test_an_unknown_producer_is_named_not_guessed(tmp_path):
    ident = scene_cache.world_identity(_campaign(tmp_path), _manifest(producer="gazebo"))
    with pytest.raises(scene_cache.SceneUnavailable, match="no scene generator is registered"):
        scene_cache._command_for(ident, 1024)


def test_generate_runs_the_generator_and_caches_the_directory(tmp_path, monkeypatch):
    """The happy path, with the container replaced by a local writer.

    Exercises the real ``run_input_generators`` -> ``shell`` generator -> atomic swap chain; only the
    *command* is faked, because a real one needs the campaign's image.
    """
    ident = scene_cache.world_identity(_campaign(tmp_path), _manifest())
    key = scene_cache.cache_key(ident)
    assert not scene_cache.is_cached(key)

    fake = tmp_path / "fake.sh"
    fake.write_text('#!/bin/sh\nmkdir -p "$1"\necho "{}" > "$1/scene.json"\n'
                    'printf x > "$1/scene.bin"\n', encoding="utf-8")
    fake.chmod(0o755)
    # No `image`: this test covers the caching semantics, not the container path -- that one was
    # measured for real against the cluster (see tests/execution/test_aux_container_transfer.py).
    monkeypatch.setattr(scene_cache, "_generate_entry",
                        lambda i, k, m: {"shell": {"out": k, "command": f"{fake} {{out}}"}})

    out = scene_cache.generate(ident, key)
    assert scene_cache.is_cached(key)
    assert os.path.isfile(os.path.join(out, "scene.json"))
    identity = json.loads(open(os.path.join(out, scene_cache.IDENTITY_FILE), encoding="utf-8").read())
    assert identity["world"] == "pkg:depot" and identity["key"] == key

    # A second call is a no-op: it must not re-run the generator.
    monkeypatch.setattr(scene_cache, "_generate_entry",
                        lambda i, k, m: (_ for _ in ()).throw(AssertionError("regenerated a hit")))
    assert scene_cache.generate(ident, key) == out


def test_a_generator_that_writes_nothing_is_not_cached(tmp_path, monkeypatch):
    """"Success" with no descriptor must not become a permanent empty cache entry.

    The generator framework catches the empty case first, with its own message ("reported success but
    wrote no files"), and this asserts that it reaches the viewer as a reason rather than as a traceback
    -- and that nothing was left behind to be served later as a hit.
    """
    ident = scene_cache.world_identity(_campaign(tmp_path), _manifest())
    key = scene_cache.cache_key(ident)
    monkeypatch.setattr(scene_cache, "_generate_entry",
                        lambda i, k, m: {"shell": {"out": k, "command": "true"}})
    with pytest.raises(scene_cache.SceneUnavailable, match="wrote no files|wrote no scene.json"):
        scene_cache.generate(ident, key)
    assert not scene_cache.is_cached(key)
    assert not os.path.isdir(scene_cache.entry_dir(key))


def test_eviction_drops_whole_entries_least_recently_used_first(tmp_path):
    root = scene_cache.cache_root()
    os.makedirs(root, exist_ok=True)
    for name, atime in (("old", 1000), ("new", 9000)):
        path = os.path.join(root, name)
        os.makedirs(path)
        with open(os.path.join(path, "scene.bin"), "wb") as fh:
            fh.write(b"x" * 1024)
        os.utime(path, (atime, atime))

    removed = scene_cache.evict(root, max_bytes=1500)
    assert removed == ["old"], "least recently used goes first"
    assert scene_cache.is_cached("new") is False  # no scene.json in the fixture
    assert os.path.isdir(os.path.join(root, "new")), "and the recent one survives"


def test_asset_path_refuses_to_escape_its_entry(tmp_path):
    key = "k"
    path = scene_cache.entry_dir(key)
    os.makedirs(path)
    with open(os.path.join(path, "scene.json"), "w", encoding="utf-8") as fh:
        fh.write("{}")
    assert scene_cache.asset_path(key, "scene.json").endswith("scene.json")
    for bad in ("../../etc/passwd", "/etc/passwd", "nope.png"):
        with pytest.raises(KeyError):
            scene_cache.asset_path(key, bad)
