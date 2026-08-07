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
  installed into the image from a wheel (``/usr/local/share/rst_nav2_example/worlds/depot_nav2.yaml``).
  A host that happens to have ``rst`` could be a different version and would render *plausible, wrong*
  geometry.
* **The cache key is the world identity**, not a file fingerprint: image identity + world + overrides +
  exporter options. The image digest pins every asset package the geometry is compiled from, so equal
  keys mean equal bytes.

Why the key is ours rather than the generator framework's
--------------------------------------------------------

``run_input_generators`` has its own staleness cache, and it is deliberately unusable here.
:func:`~robovast.common.input_generation._run_one` discards a manifest whose recorded inputs are not
visible on this host — "unverifiable inputs mean regenerate" — and a generator that ran in a container
reports paths *inside* it. ``rst-export-web --manifest`` lists 55 such paths, none of which exist here,
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

logger = logging.getLogger(__name__)

#: Longest side of a copied/re-encoded texture. 1024 keeps the depot world at ~13 MB against ~31 MB at
#: the exporter's own 2048 default, and a browser orbiting a warehouse needs no more. It is part of the
#: cache key, so changing it regenerates rather than serving the previous size.
DEFAULT_MAX_TEX_DIM = 1024

#: Bumped when this module changes what it *asks for*, so old entries are not served for new requests.
CACHE_FORMAT_VERSION = 1

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


def world_identity(campaign_dir, capture_manifest) -> dict:
    """What geometry this run needs, from its capture manifest plus the campaign's image.

    Args:
        campaign_dir: the campaign root (already local — on the cluster the caller materialises the two
            small objects it needs first).
        capture_manifest: the parsed ``capture/capture.json`` of the run being viewed.

    Returns:
        ``{producer, world, overrides, image, overrides_known}``.

    Raises:
        SceneUnavailable: when the run does not say what world it used, or the campaign does not record
            an image identity precise enough to trust a cache entry against.
    """
    from robovast.common.campaign_data import \
        read_execution_metadata  # pylint: disable=import-outside-toplevel
    from robovast.common.config import \
        SIMULATION_CONTAINER  # pylint: disable=import-outside-toplevel

    world = (capture_manifest or {}).get("world")
    if not world:
        raise SceneUnavailable(
            "this run's capture does not name the world it was recorded from, so its geometry cannot "
            "be rebuilt. Re-run with a producer that records `world` in capture.json.")

    # Deliberately not ``postprocess_job.campaign_execution_image``: that resolves an image to *run*,
    # and falling back to the tag is right there -- a run just needs something to launch. This needs an
    # image to *key a cache on*, and a mutable tag cannot do that: the same tag names different bytes
    # after a rebuild, so an entry keyed on it would serve geometry from an image that no longer exists.
    # It also accepts only ``@sha256:``, while the local lane records a bare ``sha256:`` image id --
    # immutable, just not a registry digest.
    try:
        meta = read_execution_metadata(Path(campaign_dir)) or {}
    except (OSError, ValueError) as err:
        raise SceneUnavailable(
            f"this campaign's execution metadata could not be read ({err}), so geometry cannot be "
            "built from the same packages the run used.") from err
    # The SIMULATION container's digest when the campaign had one, else the scenario's.
    # Geometry is compiled from the world the capture names, and that world -- with the
    # exporter that reads it -- lives in the simulator's image. Keying on the scenario
    # container's digest sent the build into an image with neither: on the cluster the
    # aux pod's exec failed to start, and because the Kubernetes client int()s an exec
    # status that carries a message instead of an exit code, the run view reported
    # "invalid literal for int()" rather than "executable not found".
    #
    # `image_revision` remains the fallback, so campaigns recorded before per-role
    # digests existed still resolve -- and for a stepped simulator it is the right answer
    # anyway, because there the simulator IS the scenario container.
    revisions = meta.get("image_revisions") or {}
    image = str(revisions.get(SIMULATION_CONTAINER) or meta.get("image_revision") or "")
    if not _is_immutable_image(image):
        recorded = image or meta.get("image") or "nothing"
        raise SceneUnavailable(
            f"this campaign records no immutable image identity ({recorded!r} is a mutable tag), which "
            "cannot identify the geometry it produced. Re-run the campaign to record a digest, or "
            "generate the descriptor with an execution.generate entry instead.")

    # `overrides` has three states and two of them must not be conflated: {} means "none applied",
    # absent means "this producer did not record them". Compiling the bare world for a run that
    # *varied* it renders confidently wrong geometry, so absence is carried through and reported.
    overrides_known = "overrides" in (capture_manifest or {})
    identity = {
        "producer": str((capture_manifest or {}).get("producer") or "rst"),
        "world": str(world),
        "overrides": (capture_manifest or {}).get("overrides") or {},
        "overrides_known": overrides_known,
        "image": image,
    }
    identity.update(_campaign_world(campaign_dir, str(world)))
    return identity


#: Where a campaign's ``run_files`` are mounted in a running job. A world declared as a
#: path in the ``.vast`` is recorded by the capture under this prefix, because that is
#: where the simulator read it from.
_RUN_FILE_MOUNT = "/config/"


def _campaign_world(campaign_dir, world: str) -> dict:
    """``{world_file, world_sha}`` when the world is a campaign file, else ``{}``.

    The scene cache was built for a world that lives *in the image*, installed from a
    package -- then the recorded path is valid wherever that image runs, including the aux
    container. A world declared as a path in the ``.vast`` is not that: it is a
    ``run_file``, mounted at ``/config/...`` for the duration of the job only, so passing
    the recorded path to a fresh container asks it to read something that was never there.
    The build got far enough to start the exporter and then failed on a missing file.

    The campaign archives its run files under ``_config/``, so the world still exists on
    this side and can be staged into the container as a generator input.

    Its content hash joins the cache key. For a packaged world the image digest already
    covers the bytes; for a campaign file it covers nothing, so without this two campaigns
    whose worlds share a path would serve each other's geometry.
    """
    if not world.startswith(_RUN_FILE_MOUNT):
        return {}
    local = Path(campaign_dir) / "_config" / world[len(_RUN_FILE_MOUNT):]
    if not local.is_file():
        raise SceneUnavailable(
            f"this run's world {world!r} is a campaign file, but it is not archived with "
            f"the campaign (looked in {local}), so its geometry cannot be rebuilt.")
    digest = hashlib.sha256(local.read_bytes()).hexdigest()
    return {"world_file": str(local), "world_sha": digest}


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
    from robovast.common.file_cache2 import \
        CacheKey  # pylint: disable=import-outside-toplevel

    key = CacheKey()
    # For a campaign-file world the image digest says nothing about the world's bytes.
    key.add("world_sha", identity.get("world_sha") or "")
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


def _command_for(identity: dict, max_tex_dim: int) -> str:
    """The generator command for a producer.

    One entry today, and deliberately a table rather than an entry-point group: a plugin group with a
    single member is speculative generality, and the extension point that matters already exists — a
    campaign that wants different geometry can still declare its own ``execution.generate``. When a
    *second* producer appears, this becomes a group and the mapping moves to the producers.
    """
    producer = identity["producer"]
    if producer != "rst":
        raise SceneUnavailable(
            f"no scene generator is registered for producer {producer!r}; this RoboVAST knows how to "
            "build geometry for 'rst' only.")
    # `--set k=v` per override leaf: the same dotlist form `rst sim --set` takes.
    sets = " ".join(f"--set {k}={json.dumps(v)}" for k, v in _flatten(identity["overrides"]))
    # `{inputs[0]}` when the world is a campaign file: the generator stages it into the
    # container and substitutes the path it landed at. A packaged world keeps its recorded
    # path, which is valid in the image by construction.
    world = "{inputs[0]}" if identity.get("world_file") else identity["world"]
    return (f"rst-export-web --world {world} {sets} --out {{out}} "
            f"--max-tex-dim {int(max_tex_dim)} --manifest {{out}}/.generated.json")


def _flatten(value, prefix=""):
    """Nested override dict -> ``[(dotted.key, leaf), ...]``, the form ``--set`` accepts."""
    for name, item in sorted((value or {}).items()):
        path = f"{prefix}{name}"
        if isinstance(item, dict):
            yield from _flatten(item, f"{path}.")
        else:
            yield path, item


def _generate_entry(identity: dict, key: str, max_tex_dim: int) -> dict:
    """The ``execution.generate`` entry that builds this descriptor.

    A plain ``shell`` entry with ``image`` set -- which is the documented aux-container path, so the
    container run, the input staging and the copy-back are the generator framework's, not ours.
    """
    entry = {"shell": {"out": key,
                       "image": identity["image"],
                       "command": _command_for(identity, max_tex_dim)}}
    if identity.get("world_file"):
        entry["shell"]["inputs"] = [identity["world_file"]]
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
