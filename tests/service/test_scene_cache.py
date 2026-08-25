# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The on-demand scene cache: world identity, the hit decision, and what it refuses.

The properties here are the ones that would fail *invisibly* — a cache that never hits still renders the
right picture, just slowly; a key that ignores the overrides renders the wrong picture confidently.
"""

import contextlib
import json
import os
import shlex

import pytest
import yaml

from robovast.common import run_capture
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


def _campaign(tmp_path, image="harbor/x@sha256:" + "a" * 64, backend="roqsim"):
    root = tmp_path / "campaign-2026-08-06-000000"
    (root / "_execution").mkdir(parents=True)
    (root / "_execution" / "execution.yaml").write_text(
        f"image: build:x\nimage_revision: {image}\n", encoding="utf-8")
    # The frozen `.vast`: which simulator ran, and so which backend is asked how to rebuild
    # this campaign's geometry. A campaign with none is a case of its own, below.
    (root / "_config").mkdir(parents=True, exist_ok=True)
    block = (f"    simulation:\n      backend: {backend}\n      config: pkg:depot\n"
             f"      image: {image}\n") if backend else "    sut: {}\n"
    (root / "_config" / "p.vast").write_text(
        "version: 3\nexecution:\n  mode: ros2\n  containers:\n" + block, encoding="utf-8")
    return str(root)


@contextlib.contextmanager
def _backend_named(name, backend):
    """Resolve *name* to *backend*, for a simulator that is not installed in this env."""
    from robovast.common import simulators
    real = simulators.resolve_backend
    simulators.resolve_backend = lambda n, base_dir="": backend if n == name else real(n, base_dir)
    try:
        yield
    finally:
        simulators.resolve_backend = real


def _manifest(**over):
    base = {"producer": "roqsim", "world": "pkg:depot", "overrides": {},
            "version": run_capture.FORMAT_VERSION}
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


def test_the_key_separates_capture_format_versions(tmp_path):
    """The same override document, two versions -> two entries. Nothing else can tell them apart.

    v2 changed what ``overrides`` MEANS without changing its shape: ``components.robot.lidar``
    addressed the config of the component named ``robot`` under v1 and the child component
    ``robot.lidar`` under v2. Both hash identically as data, so a key that ignored the version would
    serve one run geometry compiled for the other's world -- and it would look perfectly fine.
    """
    overrides = {"components": {"robot": {"lidar": {"rays": 4}}}}
    v1 = scene_cache.world_identity(_campaign(tmp_path / "a"),
                                    _manifest(version=1, overrides=overrides))
    v2 = scene_cache.world_identity(_campaign(tmp_path / "b"),
                                    _manifest(version=2, overrides=overrides))
    assert scene_cache.cache_key(v1) != scene_cache.cache_key(v2)


def test_a_capture_newer_than_this_code_is_refused(tmp_path):
    """The refusal that matters most is HERE, not in the viewer.

    A viewer that cannot read a capture simply fails to animate. This side reads the manifest's
    ``overrides`` to decide which geometry the run gets, so a version whose fields mean something
    this code has not seen would be keyed and compiled anyway, and the picture would look right.
    """
    newer = run_capture.FORMAT_VERSION + 1
    with pytest.raises(scene_cache.SceneUnavailable, match=f"format version {newer}"):
        scene_cache.world_identity(_campaign(tmp_path), _manifest(version=newer))


def test_a_manifest_without_a_version_reads_as_the_oldest_format(tmp_path):
    """Absent is v1, not a refusal: the oldest format is the only safe assumption about a manifest
    that does not declare one, and refusing would strand a capture this code can in fact read."""
    manifest = _manifest()
    del manifest["version"]
    ident = scene_cache.world_identity(_campaign(tmp_path), manifest)
    assert ident["capture_version"] == 1


def test_a_workspace_and_a_campaign_naming_one_world_still_share_an_entry(tmp_path):
    """The warm-share the docstring promises: open the Config tab, then a run view, and the second
    is already built. A `.vast`'s `sim:` block has no capture to read a version from and is written
    against TODAY's override grammar, so it is keyed at the current version -- which is what keeps
    this true rather than splitting the two apart the moment a version entered the key.
    """
    image = "harbor/x@sha256:" + "a" * 64
    raw = {"execution": {"containers": {"simulation": {"image": image, "config": "pkg:depot"}}}}
    workspace = scene_cache.workspace_world_identity(str(tmp_path / "ws"), raw)
    campaign = scene_cache.world_identity(_campaign(tmp_path, image=image), _manifest())
    assert workspace["capture_version"] == run_capture.FORMAT_VERSION
    assert scene_cache.cache_key(workspace) == scene_cache.cache_key(campaign)


def test_the_key_separates_exporter_options(tmp_path):
    ident = scene_cache.world_identity(_campaign(tmp_path), _manifest())
    assert scene_cache.cache_key(ident, 1024) != scene_cache.cache_key(ident, 2048)


@pytest.mark.requires_simulator
def test_overrides_travel_as_a_file_not_as_argv(tmp_path):
    """A campaign's overrides go in through --override, the spelling the RUN already uses.

    They were flattened onto repeated ``--set`` until a campaign varied something structured.
    """
    ident = scene_cache.world_identity(
        _campaign(tmp_path),
        _manifest(overrides={"plugins": {"floorplan": {"size": 4.0}}, "sim": {"pacing": "asap"}}))
    # The real mount, not a literal: where the file appears is a lane constraint (the cluster
    # only mounts AUX_MOUNTABLE_PATHS), so a test carrying its own path hid a mismatch once.
    cmd = scene_cache._command_for(ident, 1024, scene_cache._OVERRIDES_MOUNT)
    args = shlex.split(cmd)
    assert "--override" in args
    assert args[args.index("--override") + 1] == scene_cache._OVERRIDES_MOUNT
    assert "--set" not in args, "argv is not the channel for a recorded override tree"
    assert "--manifest {out}/.generated.json" in cmd, "the tool reports its own inputs"


def test_a_structured_override_survives(tmp_path):
    """The case argv cannot carry: a LIST OF MAPPINGS, i.e. an obstacle population.

    Flattened onto ``--set`` it rendered as ``plugins.boxes.instances=[{"name":...,"pos":...}]``,
    which is not a dotlist value -- the exporter read ``"pos"`` as a key with its quotes still
    attached and died with ``KeyError: '"pos"'``. It fails only when somebody opens the run view,
    so it reads as "this campaign has no 3D geometry" rather than as a quoting bug.
    """
    instances = [{"name": "dynamic_0", "pos": [1.03, 0.55], "size": [0.5, 0.5, 1.0]}]
    ident = scene_cache.world_identity(
        _campaign(tmp_path),
        _manifest(overrides={"plugins": {"dynamic_obstacles": {"instances": instances}}}))
    path = scene_cache._overrides_file(ident, "somekey")
    assert path, "overrides present means a document to hand the exporter"
    with open(path, encoding="utf-8") as handle:
        assert yaml.safe_load(handle)["plugins"]["dynamic_obstacles"]["instances"] == instances


@pytest.mark.requires_simulator
def test_no_overrides_means_no_file_and_no_flag(tmp_path):
    """A world compiled as declared needs neither, and must not be handed an empty document."""
    ident = scene_cache.world_identity(_campaign(tmp_path), _manifest(overrides={}))
    assert scene_cache._overrides_file(ident, "somekey") is None
    assert "--override" not in shlex.split(scene_cache._command_for(ident, 1024, None))


@pytest.mark.requires_simulator
def test_the_command_is_the_backends_to_give(tmp_path):
    """Which exporter builds geometry is the simulator's business, not the service's.

    Asked of the backend the campaign already names, so a second simulator needs no table
    here -- only the descriptor format stays RoboVAST's.
    """
    ident = scene_cache.world_identity(_campaign(tmp_path), _manifest())
    assert ident["backend"] == "roqsim"
    assert "roqsim-export-web" in scene_cache._command_for(ident, 1024)


def test_a_simulator_that_exports_no_geometry_says_so(tmp_path):
    """A backend with no exporter (Gazebo has none) is a normal answer, not a missing tool."""
    class _Mute:
        SUPPORTED_SHAPES = ("ros", "stepped")
        CONFIG_CLASS = None

        def scene_export(self, cfg, execution, **kw):
            return None

    ident = scene_cache.world_identity(_campaign(tmp_path, backend="mute"), _manifest())
    with pytest.raises(scene_cache.SceneUnavailable, match="exports no scene descriptor"):
        with _backend_named("mute", _Mute()):
            scene_cache._command_for(ident, 1024)


def test_a_campaign_naming_no_simulator_is_named_not_guessed(tmp_path):
    ident = scene_cache.world_identity(_campaign(tmp_path, backend=None), _manifest())
    with pytest.raises(scene_cache.SceneUnavailable, match="none declared"):
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
