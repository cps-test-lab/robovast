# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""On-demand 3D scene descriptors for the run view, cached across campaigns.

A run view needs two artifacts (see ``docs/run_capture.rst``): the **capture** (motion), which the run
itself writes, and the **scene descriptor** (geometry), which is what this module produces.

It used to be produced eagerly, by an ``execution.generate`` entry at campaign preparation: 5-9 s and
13-31 MB for every campaign, whether or not anyone ever opened the 3D view, and impossible to get right
for a sweep whose world varies per configuration (generation has no notion of a config name). Here it is
produced when somebody actually looks, and cached by **world identity** so the second look — and every
other campaign that used the same world — costs nothing.

Three things make that work:

* **A run says which world it needs.** The capture manifest records ``world`` and ``overrides``, so
  nothing has to be declared in the ``.vast`` and a per-config world needs no special handling.
* **Generation runs in the campaign's own pinned image.** The world is generally not on this host: it is
  installed into the image from a wheel (``/usr/local/share/roqsim_nav2_example/worlds/depot_nav2.yaml``).
  A host that happens to have ``roqsim`` could be a different version and would render *plausible, wrong*
  geometry.
* **The cache key is the world identity**, not a file fingerprint: image identity + world + overrides +
  exporter options. The image digest pins every asset package the geometry is compiled from, so equal
  keys mean equal bytes.

Why the key is ours rather than the generator framework's
--------------------------------------------------------

``run_input_generators`` has its own staleness cache, and it is deliberately unusable here.
:func:`~robovast.common.input_generation._run_one` discards a manifest whose recorded inputs are not
visible on this host — "unverifiable inputs mean regenerate" — and a generator that ran in a container
reports paths *inside* it. ``roqsim-export-web --manifest`` lists 55 such paths, none of which exist here,
so that cache would never hit: every viewer would recompile the world. So generation is invoked with
``use_cache=False`` and this module decides hits itself, which it can do without running anything.

What is reused, and what is not
-------------------------------

Reused: :func:`~robovast.common.input_generation.run_input_generators` for the container run, staging and
atomic swap; the ``shell`` generator's ``image`` support for the aux container;
:class:`~robovast.common.file_cache2.CacheKey` for fingerprinting; ``read_execution_metadata``
for ``execution.yaml``.

Not reused: :class:`~robovast.common.file_cache2.FileCache2` itself. It stores **one file** per key (a
directory goes in as a tarball), and this cache is served over HTTP as a directory — ``scene.json`` plus
``scene.bin`` plus a PNG per texture, fetched as relative siblings. Tarring on write and extracting on
read to satisfy an API we would immediately undo is worse than owning a directory per key.
"""

import hashlib
import json
import logging
import os
import shutil
import threading
from pathlib import Path

import yaml

from robovast.common.simulators import backend_name

logger = logging.getLogger(__name__)

#: Longest side of a copied/re-encoded texture. 1024 keeps the depot world at ~13 MB against ~31 MB at
#: the exporter's own 2048 default, and a browser orbiting a warehouse needs no more. It is part of the
#: cache key, so changing it regenerates rather than serving the previous size.
DEFAULT_MAX_TEX_DIM = 1024

#: Bumped when this module changes what it *asks for*, so old entries are not served for new requests.
#: 2: the whole ``_config/`` tree is staged and the key covers it, where 1 hashed the world file alone.
CACHE_FORMAT_VERSION = 2

#: Files a descriptor must have to count as complete. A half-written directory must never be a hit; the
#: generator's atomic swap makes that nearly impossible, but "nearly" is not a cache invariant.
REQUIRED_OUTPUTS = ("scene.json", "scene.bin")

#: Written beside each entry: what it was generated from, for a human reading the cache directory.
IDENTITY_FILE = ".identity.json"

#: Per-key locks, so two viewers opening the same uncached world generate once and the second waits.
#: Mirrors ``ClusterService._fetch_locks``: without it the *expensive* path is the one that races.
_locks: "dict[str, threading.Lock]" = {}
_locks_guard = threading.Lock()

#: Why each key's last generation attempt failed. Generation runs on a background thread, so the reason
#: has nowhere else to go: the request that started it has already returned its ActionResult.
_failures: "dict[str, str]" = {}
_failures_guard = threading.Lock()


class SceneUnavailable(RuntimeError):
    """Geometry cannot be produced for this run, with a reason a viewer can show verbatim."""


def cache_root() -> str:
    """Where generated descriptors live, shared by every campaign.

    Durable on purpose. ``ClusterService._cache_dir`` uses ``/tmp`` because it caches *fetches* for a
    pod that may be replaced at any moment; this cache exists so that work done once is never redone,
    and putting it in ``/tmp`` would quietly reintroduce the cost the whole feature removes — a reboot
    or a service restart and every world recompiles, looking correct the whole time, just slow.
    """
    root = os.environ.get("ROBOVAST_SCENE_CACHE")
    if not root:
        root = os.path.join(os.path.expanduser("~"), ".robovast", "cache", "scenes")
    return root


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def world_identity(campaign_dir, capture_manifest, resolve_digest=None,
                   config_name: str = "") -> dict:
    """What geometry this run needs, from its capture manifest plus the campaign's image.

    Args:
        campaign_dir: the campaign root (already local — on the cluster the caller materialises the two
            small objects it needs first).
        capture_manifest: the parsed ``capture/capture.json`` of the run being viewed.
        resolve_digest: ``ref -> digest | None``, from the lane that can answer it (locally
            ``docker inspect``). Lets a campaign that recorded only a declared *tag* still be
            keyed on bytes; without it such a campaign is refused rather than guessed at.
        config_name: the configuration this run belongs to. Needed because a world may be a
            file the *configuration* owns rather than the campaign -- see
            :func:`campaign_world_rel`.

    Returns:
        ``{producer, world, overrides, image, overrides_known}``.

    Raises:
        SceneUnavailable: when the run does not say what world it used, or the campaign does not record
            an image identity precise enough to trust a cache entry against.
    """
    from robovast.common.campaign_data import (  # pylint: disable=import-outside-toplevel
        RoleImageUnavailable, campaign_role_image)
    from robovast.common.config import \
        SIMULATION_CONTAINER  # pylint: disable=import-outside-toplevel

    world = (capture_manifest or {}).get("world")
    if not world:
        raise SceneUnavailable(
            "this run's capture does not name the world it was recorded from, so its geometry cannot "
            "be rebuilt. Re-run with a producer that records `world` in capture.json.")

    # Geometry is compiled from the world the capture names, and that world -- with the exporter that
    # reads it -- lives in the SIMULATION image, so that is the role asked for. `campaign_role_image`
    # owns the whole answer, identically on both lanes, and refuses rather than substituting another
    # role's image: keying on the scenario container's digest sent the build into an image with
    # neither the world nor the exporter, which surfaced locally as a bare `exit status 127` and on
    # the cluster as "invalid literal for int()" (the Kubernetes client int()s an exec status that
    # carries a message instead of an exit code).
    #
    # Deliberately not ``postprocess_job.campaign_execution_image``: that resolves an image to *run*,
    # where falling back to a tag is correct -- a run just needs something to launch. This keys a
    # cache, and a mutable tag cannot: the same tag names different bytes after a rebuild, so an entry
    # keyed on it would serve geometry from an image that no longer exists.
    try:
        image = campaign_role_image(Path(campaign_dir), SIMULATION_CONTAINER,
                                    resolve_digest=resolve_digest)
    except FileNotFoundError as err:
        raise SceneUnavailable(
            f"this campaign's execution metadata could not be read ({err}), so geometry cannot be "
            "built from the same packages the run used.") from err
    except RoleImageUnavailable as err:
        raise SceneUnavailable(str(err)) from err
    if not _is_immutable_image(image):
        raise SceneUnavailable(
            f"the image resolved for this campaign's simulator ({image!r}) does not name immutable "
            "bytes, so it cannot identify the geometry it produced. Re-run the campaign to record a "
            "digest, or generate the descriptor with an execution.generate entry instead.")

    # `overrides` has three states and two of them must not be conflated: {} means "none applied",
    # absent means "this producer did not record them". Compiling the bare world for a run that
    # *varied* it renders confidently wrong geometry, so absence is carried through and reported.
    overrides_known = "overrides" in (capture_manifest or {})
    execution = _campaign_execution(campaign_dir)
    identity = {
        "producer": str((capture_manifest or {}).get("producer") or "roqsim"),
        "world": str(world),
        "overrides": (capture_manifest or {}).get("overrides") or {},
        "overrides_known": overrides_known,
        "image": image,
        # Which simulator this campaign ran, so the command that rebuilds its geometry can be
        # asked of that backend rather than assumed here.
        "execution": execution,
        "backend": backend_name(execution),
    }
    identity.update(_campaign_world(campaign_dir, str(world), config_name))
    # A second tier, when the world is the campaign's but an override names a file this
    # configuration owns. Asked after the world's own tier, because whether it is needed
    # depends on that answer.
    identity.update(_per_config_tree(campaign_dir, identity["overrides"], config_name,
                                     per_config_world=bool(identity.get("config_mount"))))
    return identity


#: Where a campaign's ``run_files`` are mounted in a running job. A world declared as a
#: path in the ``.vast`` is recorded by the capture under this prefix, because that is
#: where the simulator read it from.
_RUN_FILE_MOUNT = "/config/"

#: Where a campaign archives those same files on the results store.
_CONFIG_DIR = "_config"


def campaign_world_rel(world: str, config_name: str = "") -> str | None:
    """The campaign-relative path of a world that is a run file, else ``None``.

    One place knows how a recorded ``/config/...`` world maps back onto the results tree:
    this. The cluster lane needs it to materialise the objects before resolving identity,
    and :func:`_campaign_world` needs it to find the file.

    **Two tiers**, because a world can belong to the campaign or to one configuration:

    ``/config/<rel>``                 -> ``_config/<rel>``
        A ``run_file``: declared in the ``.vast`` (or by the backend) and mounted campaign-wide.

    ``/config/<config-name>/<rel>``   -> ``<config-name>/_config/<rel>``
        A file a *variation generated* for this configuration -- a floorplan baked per cell,
        say. Mounted under its own prefix precisely so several configurations' files cannot
        collide in one packed job, which is why the mapping back is not the campaign one.

    Getting this wrong is not a broken path but a missing 3D scene, so the per-config form
    is recognised by the configuration's own name rather than guessed from the shape of the
    path -- a campaign-level ``files/nav2_params.yaml`` has a directory part too.
    """
    if not world.startswith(_RUN_FILE_MOUNT):
        return None
    rest = world[len(_RUN_FILE_MOUNT):]
    prefix = f"{config_name}/" if config_name else None
    if prefix and rest.startswith(prefix):
        return f"{config_name}/{_CONFIG_DIR}/{rest[len(prefix):]}"
    return f"{_CONFIG_DIR}/{rest}"


def _campaign_world(campaign_dir, world: str, config_name: str = "") -> dict:
    """``{world_file, config_root, config_sha}`` when the world is a campaign file, else ``{}``.

    The scene cache was built for a world that lives *in the image*, installed from a
    package -- then the recorded path is valid wherever that image runs, including the aux
    container. A world declared as a path in the ``.vast`` is not that: it is a
    ``run_file``, mounted at ``/config/...`` for the duration of the job only, so passing
    the recorded path to a fresh container asks it to read something that was never there.

    **A world is not one file.** It names meshes, colliders and maps, and it names them by
    the path they had in the job -- ``/config/...``, because that is where the campaign's
    run files were mounted. Staging the YAML alone therefore fails on the first thing it
    references, which is what it did. So the whole of ``_config/`` is staged and mounted
    back at ``/config``, and the world compiles from exactly the paths the run compiled it
    from. Nothing here has to know which keys of which plugin hold a path.

    The tree's digest joins the cache key. For a packaged world the image digest already
    covers the bytes; for campaign files it covers nothing, so without this two campaigns
    whose worlds share a path would serve each other's geometry -- and a campaign whose
    mesh changed would serve the old shape.
    """
    rel = campaign_world_rel(world, config_name)
    if rel is None:
        return {}
    per_config = rel.startswith(f"{config_name}/") if config_name else False
    root = (Path(campaign_dir) / config_name / _CONFIG_DIR if per_config
            else Path(campaign_dir) / _CONFIG_DIR)
    local = Path(campaign_dir) / rel
    if not local.is_file():
        raise SceneUnavailable(
            f"this run's world {world!r} is a campaign file, but it is not archived with "
            f"the campaign (looked in {local}), so its geometry cannot be rebuilt.")
    result = {"world_file": str(local), "config_root": str(root),
              "config_sha": _tree_sha(root)}
    if per_config:
        # Where the tree goes back in the build container. A generated world names its
        # meshes by the path it had in the JOB, which for a per-configuration file is
        # `/config/<config-name>/...` -- so mounting its tree at `/config` like a campaign
        # one would compile a world whose every reference is off by that segment.
        result["config_mount"] = f"{_RUN_FILE_MOUNT}{config_name}"
    return result


def _per_config_tree(campaign_dir, overrides, config_name: str, per_config_world: bool) -> dict:
    """The configuration's own ``_config/`` tree, when an OVERRIDE reaches into it.

    The world and the files it names need not belong to the same tier. A campaign may ship
    ONE world and give each configuration its environment through the ``sim`` channel --
    which is what that channel is for -- and then the world is a campaign-level run file
    while ``plugins.floorplan.mesh`` points at ``/config/<config-name>/...``, a file a
    variation generated for this cell alone. Deciding the mount from the world's own tier
    (:func:`_campaign_world`) stages only ``<campaign>/_config``, and the export fails on
    the first path the world resolves -- not with a bad path but with no 3D scene at all,
    for exactly the campaigns the ``sim`` channel makes possible.

    Nothing here knows which keys hold a path: an override VALUE that starts with this
    configuration's mount prefix is the whole test, so a plugin key nobody has thought of
    is served identically.

    Returns ``{}`` when no override reaches into the tree, or when the world is itself
    per-configuration (its tree is already mounted at that prefix, so the paths resolve).
    """
    if not config_name or per_config_world:
        return {}
    prefix = f"{_RUN_FILE_MOUNT}{config_name}/"
    if not any(isinstance(v, str) and v.startswith(prefix) for v in _leaf_values(overrides)):
        return {}
    root = Path(campaign_dir) / config_name / _CONFIG_DIR
    if not root.is_dir():
        raise SceneUnavailable(
            f"this run's world is overridden with a file under {prefix!r}, but the "
            f"configuration's archived inputs are missing (looked in {root}), so its "
            "geometry cannot be rebuilt.")
    # Its digest joins the cache key for the same reason the campaign tree's does: the image
    # digest covers none of these bytes, so without it a configuration whose generated mesh
    # changed would be served the old shape.
    return {"extra_root": str(root), "extra_mount": f"{_RUN_FILE_MOUNT}{config_name}",
            "extra_sha": _tree_sha(root)}


def _leaf_values(value):
    """Every scalar in a nested mapping/sequence, so an override value is found wherever it sits."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaf_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _leaf_values(item)
    else:
        yield value


def _campaign_execution(campaign_dir) -> dict:
    """The frozen ``.vast``'s ``execution`` block -- which simulator this campaign ran.

    From the ``.vast`` rather than ``_execution/execution.yaml``, which records what the run
    *did* (images, timing) and not what it was configured with. Absent or unreadable is not
    an error here: the caller turns "no backend" into a reason a user can act on, and a
    campaign is worth looking at even when its config cannot be parsed.
    """
    try:
        from robovast.common.results_utils import \
            campaign_vast  # pylint: disable=import-outside-toplevel
        raw = yaml.safe_load(campaign_vast(campaign_dir).read_text(encoding="utf-8")) or {}
        return (raw.get("execution") or {}) if isinstance(raw, dict) else {}
    except Exception as err:  # noqa: BLE001 - a missing/broken .vast is reported downstream
        logger.debug("no execution block for %s: %s", campaign_dir, err)
        return {}


def _tree_sha(root: Path) -> str:
    """Content fingerprint of a directory: every file's relative path and bytes, sorted.

    Sorted so it does not depend on directory order, and over paths as well as bytes so a
    renamed file is a different tree.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _is_immutable_image(image: str) -> bool:
    """Whether *image* names bytes that cannot change under it.

    Two shapes qualify: a registry digest (``repo@sha256:…``, what the cluster records) and a bare local
    image id (``sha256:…``, what ``docker inspect .Id`` gives the local lane). A tag never does.
    """
    if not image:
        return False
    if "@sha256:" in image:
        return True
    return image.startswith("sha256:") and len(image) >= 20


def cache_key(identity: dict, max_tex_dim: int = DEFAULT_MAX_TEX_DIM) -> str:
    """Fingerprint of the world identity — the cache's hit decision, computable without generating.

    Values only, never :meth:`CacheKey.add_file`: none of the world's files exist on this host, which
    is the entire reason generation happens in a container.
    """
    from robovast.common.file_cache2 import CacheKey  # pylint: disable=import-outside-toplevel

    key = CacheKey()
    # For a campaign-file world the image digest says nothing about the bytes -- of the
    # world or of anything it references, which is why this covers the whole tree.
    key.add("config_sha", identity.get("config_sha") or "")
    # The configuration's own tree, when an override reaches into it. Same reasoning as
    # `config_sha`: these bytes are covered by nothing else, so two configurations of one
    # campaign -- same world, same image, different generated mesh -- would otherwise share
    # a key and be served each other's geometry.
    key.add("extra_sha", identity.get("extra_sha") or "")
    key.add("scene_cache_version", CACHE_FORMAT_VERSION)
    key.add("image", identity["image"])
    key.add("world", identity["world"])
    key.add("overrides", identity["overrides"])
    key.add("max_tex_dim", int(max_tex_dim))
    key.add("producer", identity["producer"])
    return key.fingerprint()


def entry_dir(key: str) -> str:
    return os.path.join(cache_root(), key)


def is_cached(key: str) -> bool:
    """True when this key has a complete descriptor on disk."""
    path = entry_dir(key)
    return all(os.path.isfile(os.path.join(path, name)) for name in REQUIRED_OUTPUTS)


def entry_bytes(key: str) -> int:
    path = entry_dir(key)
    if not os.path.isdir(path):
        return 0
    return sum(os.path.getsize(os.path.join(dirpath, name))
               for dirpath, _dirs, names in os.walk(path) for name in names)


def is_generating(key: str) -> bool:
    """True while some request holds this key's lock."""
    return _lock_for(key).locked()


def last_failure(key: str) -> str:
    """Why this key's last generation attempt failed, or ``""``.

    A failed build must reach the viewer: without this the status falls back to "geometry has not been
    built for this world yet", which is indistinguishable from never having asked — so the panel offers
    Retry forever while the actual reason sits in the service log. Kept in memory rather than on disk
    deliberately: a restart re-reads the world and may well succeed (a missing image has since been
    pulled), so a stale reason would be worse than none.
    """
    with _failures_guard:
        return _failures.get(key, "")


def record_failure(key: str, reason: str) -> None:
    """Remember why a generation attempt failed, for :func:`last_failure`."""
    with _failures_guard:
        _failures[key] = reason


def clear_failure(key: str) -> None:
    """Forget a previous failure — a retry starts from a clean slate, as does a success."""
    with _failures_guard:
        _failures.pop(key, None)


def _command_for(identity: dict, max_tex_dim: int, overrides_file: str | None = None) -> str:
    """The command that compiles this world, asked of the campaign's simulator backend.

    Which exporter builds geometry, and how it spells its arguments, is the *simulator's*
    business -- so it is answered by the backend the campaign already names rather than by a
    table here. What stays RoboVAST's is the descriptor format the command has to produce
    (``scene.json`` + ``scene.bin``, and the ``.generated.json`` manifest).

    The world is passed as RECORDED, in both cases. A packaged world's path is valid in the
    image by construction; a campaign file's is valid because the campaign's ``_config/`` is
    mounted back at ``/config``, where the run had it -- so the world resolves its own
    references (meshes, colliders) exactly as it did then. See :func:`_campaign_world`.
    """
    from robovast.common.simulators import \
        scene_export_command  # pylint: disable=import-outside-toplevel

    execution = identity.get("execution") or {}
    name = identity.get("backend")
    try:
        command = scene_export_command(execution, world=identity["world"],
                                       max_tex_dim=int(max_tex_dim),
                                       overrides=identity["overrides"],
                                       overrides_file=overrides_file)
    except Exception as err:  # noqa: BLE001 - reported as a reason, not a traceback
        raise SceneUnavailable(
            f"the simulator backend {name!r} could not say how to build this campaign's "
            f"geometry: {err}") from err
    if not command:
        raise SceneUnavailable(
            f"this campaign's simulator ({name or 'none declared'}) exports no scene "
            "descriptor, so there is no geometry to build. A campaign can still declare its "
            "own execution.generate entry to produce one.")
    return command


#: Where a staged overrides document appears inside the build container. Outside ``/config``
#: on purpose: the campaign trees mount there, and an input nested in another input's mount
#: has to be copied into it rather than bound (see ``stage_for_container``) -- a neutral path
#: keeps this one an ordinary mount whatever the world's tier turns out to be.
#:
#: ``/aux``, not ``/tmp``: on the cluster a fixed mount is an emptyDir the *Pod* declares, so
#: the directory has to be one of ``AUX_MOUNTABLE_PATHS`` -- ``/tmp`` was neither mountable
#: (the scene build failed with "a new path has to be added to AUX_MOUNTABLE_PATHS") nor a
#: path worth mounting, since an emptyDir over it would shadow whatever the aux image keeps
#: there. The local lane bind-mounts the file and never saw the difference.
_OVERRIDES_MOUNT = "/aux/roqsim_scene_overrides.yaml"


def _overrides_file(identity: dict, key: str) -> str | None:
    """The run's overrides as a YAML file, or ``None`` when there are none.

    A campaign's overrides are a nested tree and argv cannot carry one: a list of obstacle
    instances flattened onto ``--set`` reached the exporter as ``KeyError: '"pos"'``, and
    only when somebody opened the run view. The run itself already solved this by passing a
    file, so the export uses the same spelling.

    Written beside the cache rather than into the entry, which is the generator's output.
    Keyed like the entry, so two configurations of one campaign cannot share one.
    """
    import yaml  # pylint: disable=import-outside-toplevel
    overrides = identity.get("overrides") or {}
    if not overrides:
        return None
    path = os.path.join(cache_root(), "_overrides", f"{key}.yaml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(overrides, handle, default_flow_style=False)
    return path


def _generate_entry(identity: dict, key: str, max_tex_dim: int) -> dict:
    """The ``execution.generate`` entry that builds this descriptor.

    A plain ``shell`` entry with ``image`` set -- which is the documented aux-container path, so the
    container run, the input staging and the copy-back are the generator framework's, not ours.
    """
    overrides_file = _overrides_file(identity, key)
    entry = {"shell": {"out": key,
                       "image": identity["image"],
                       "command": _command_for(identity, max_tex_dim,
                                               overrides_file and _OVERRIDES_MOUNT)}}
    if identity.get("config_root"):
        # The tree, at the path the run had it. Not the world file alone: it names the rest
        # by absolute `/config/...` path, so the mount is what makes those resolve.
        inputs = [identity["config_root"]]
        mount_at = {identity["config_root"]:
                    identity.get("config_mount") or _RUN_FILE_MOUNT.rstrip("/")}
        # Both tiers when they differ: the campaign's tree at `/config`, and this
        # configuration's at `/config/<config-name>`, which is where an override that
        # varies the world per cell points. `inputs` is a list and `mount_at` a mapping
        # precisely so a build can need more than one.
        if identity.get("extra_root"):
            inputs.append(identity["extra_root"])
            mount_at[identity["extra_root"]] = identity["extra_mount"]
        entry["shell"]["inputs"] = inputs
        entry["shell"]["mount_at"] = mount_at
    if overrides_file:
        entry["shell"].setdefault("inputs", []).append(overrides_file)
        entry["shell"].setdefault("mount_at", {})[overrides_file] = _OVERRIDES_MOUNT
    return entry


def generate(identity: dict, key: str, max_tex_dim: int = DEFAULT_MAX_TEX_DIM,
             runner_context=None, progress=None) -> str:
    """Build the descriptor for *identity* into the cache and return its directory.

    Idempotent and safe under concurrency: the key's lock is held for the whole build, and a caller that
    finds the entry already complete returns it without generating.

    *runner_context* is a zero-argument callable returning a **context manager** that yields the
    generator's ``container_runner_factory`` — a context rather than a bare factory because on the
    cluster the factory is backed by a pod, and whoever creates that pod has to close it. The local lane
    passes nothing: an absent factory makes the generator fall back to an ephemeral ``docker run``,
    which is exactly right there.
    """
    import contextlib

    from robovast.common.input_generation import \
        run_input_generators  # pylint: disable=import-outside-toplevel

    progress = progress or logger.info
    out_dir = entry_dir(key)
    with _lock_for(key):
        if is_cached(key):
            return out_dir
        os.makedirs(cache_root(), exist_ok=True)
        entry = _generate_entry(identity, key, max_tex_dim)
        # use_cache=False is not an optimisation: a containerized generator's manifest names paths that
        # do not exist here, so that cache can never hit (see the module docstring). Our own key already
        # decided this is a miss.
        try:
            context = runner_context() if runner_context else contextlib.nullcontext(None)
            with context as factory:
                run_input_generators(cache_root(), [entry], progress_update_callback=progress,
                                     container_runner_factory=factory, use_cache=False)
        except Exception as err:  # noqa: BLE001 - every failure must reach the viewer as a reason
            shutil.rmtree(out_dir, ignore_errors=True)
            raise SceneUnavailable(f"could not build the scene descriptor: {err}") from err
        if not is_cached(key):
            shutil.rmtree(out_dir, ignore_errors=True)
            raise SceneUnavailable(
                "the scene generator reported success but wrote no scene.json/scene.bin — refusing to "
                "cache an incomplete descriptor.")
        _write_identity(out_dir, identity, key, max_tex_dim)
        clear_failure(key)
        evict(cache_root())
        return out_dir


def _write_identity(out_dir, identity, key, max_tex_dim) -> None:
    """Record what this entry was built from, so the cache directory is readable by a human."""
    payload = dict(identity, key=key, max_tex_dim=int(max_tex_dim),
                   scene_cache_version=CACHE_FORMAT_VERSION)
    try:
        with open(os.path.join(out_dir, IDENTITY_FILE), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
    except OSError as err:  # pragma: no cover - a note, never worth failing a good build for
        logger.debug("could not write %s: %s", IDENTITY_FILE, err)


#: Cache ceiling. Above it, whole entries go, least-recently-*used* first (see :func:`evict`).
DEFAULT_MAX_CACHE_BYTES = 4 * 1024 ** 3


def evict(root: str, max_bytes: int | None = None) -> list:
    """Trim the cache to *max_bytes*, dropping least-recently-used entries whole.

    No cache in this codebase evicts, and at 13-31 MB per world this one is a slow disk leak without it.
    Entries are dropped whole because half a descriptor is not a smaller descriptor — the loader fetches
    ``scene.bin`` and every texture as siblings of ``scene.json``.

    Recency is the entry directory's ``st_atime``, refreshed by :func:`touch`, so a world in daily use
    survives and one nobody has opened since a paper was written does not.
    """
    if max_bytes is None:
        max_bytes = int(os.environ.get("ROBOVAST_SCENE_CACHE_BYTES") or DEFAULT_MAX_CACHE_BYTES)
    if not os.path.isdir(root):
        return []
    entries = []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path) or name.startswith("."):
            continue
        try:
            entries.append((os.stat(path).st_atime, entry_bytes(name), name))
        except OSError:
            continue
    total = sum(size for _atime, size, _name in entries)
    removed = []
    for _atime, size, name in sorted(entries):
        if total <= max_bytes:
            break
        # Never evict an entry a request is building or reading right now.
        if _lock_for(name).locked():
            continue
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)
        total -= size
        removed.append(name)
    if removed:
        logger.info("scene cache: evicted %d entr%s to stay under %d bytes",
                    len(removed), "y" if len(removed) == 1 else "ies", max_bytes)
    return removed


def touch(key: str) -> None:
    """Mark an entry as used, so eviction sees reads and not only writes."""
    path = entry_dir(key)
    try:
        os.utime(path, None)
    except OSError:
        pass


def asset_path(key: str, rel: str) -> str:
    """Resolve a file within a cache entry, refusing anything that escapes it."""
    root = os.path.realpath(entry_dir(key))
    full = os.path.realpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        raise KeyError(rel)
    if not os.path.isfile(full):
        raise KeyError(rel)
    return full


def short_key(key: str) -> str:
    """A short form for logs and URLs; the full fingerprint stays the directory name."""
    return hashlib.sha256(key.encode()).hexdigest()[:12]
