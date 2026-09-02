# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Reading a build lock without a container runtime.

The lock is baked into the image, and the usual reader inspects a local copy of it. The
controller pod has no runtime, so on the cluster lane that reader could never answer -- and its
silence was taken for "this image carries no lock". The campaign's own persisted copy, the only
one that outlives the image, was therefore never written for any campaign that ran on a cluster:
it stayed reproducible exactly as long as its images did, which is the one condition the lock
exists to remove.

So the lock is read straight out of the registry instead: the manifest, then the trailing layer
blobs. No pull, no runtime.
"""

import gzip
import io
import tarfile

from robovast.common.execution import BUILD_MANIFEST_DIR
from robovast.execution.cluster_execution import registry_client
from robovast.service.image_build import parse_build_manifest_files

_PREFIX = BUILD_MANIFEST_DIR.lstrip("/")


def _layer(files: dict, *, compress=True, prefix=None) -> bytes:
    """A layer blob carrying *files*, as the registry stores one."""
    raw = io.BytesIO()
    at = _PREFIX if prefix is None else prefix
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{at}/{name}".lstrip("/"))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    blob = raw.getvalue()
    return gzip.compress(blob) if compress else blob


class _Resp:
    def __init__(self, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code


def _wire(monkeypatch, layers, *, sizes=None):
    """Serve *layers* (newest last, as a manifest lists them) and count what got fetched."""
    fetched = []
    blobs = {f"sha256:{i:064x}": blob for i, blob in enumerate(layers)}
    manifest = {"layers": [
        {"digest": digest, "size": (sizes or {}).get(digest, len(blob))}
        for digest, blob in blobs.items()]}

    monkeypatch.setattr(registry_client, "_manifest_json",
                        lambda *a, **k: manifest)

    def _request(_host, path, **_kw):
        digest = path.rsplit("/", 1)[-1]
        fetched.append(digest)
        return _Resp(blobs[digest]) if digest in blobs else _Resp(status_code=404)

    monkeypatch.setattr(registry_client, "_registry_request", _request)
    return fetched


def test_files_written_by_separate_run_steps_are_all_found(monkeypatch):
    """The regression this guards: each manifest file is written by its own ``RUN``, so each
    lands in its OWN layer. A reader that stops at the first layer carrying one returns
    whichever happened to be last and silently loses the rest -- and a lock missing its apt
    half pins nothing, while still looking like a lock."""
    _wire(monkeypatch, [
        _layer({"apt.txt": "tree=2.2.1-1\nzlib1g=1:1.3.dfsg-3.1\n"}),
        _layer({"pip.txt": "numpy==1.26.4\n"}),
    ])

    lock = parse_build_manifest_files(
        registry_client.manifest_build_lock("reg.example.com/o/sut:t"))

    assert lock["apt"]["tree"] == "2.2.1-1"
    assert lock["pip"]["numpy"] == "1.26.4"


def test_the_newest_layer_wins(monkeypatch):
    """Layers are walked newest first: a rebuilt manifest must override the one it replaced,
    not be overridden by it."""
    _wire(monkeypatch, [
        _layer({"apt.txt": "tree=1.0.0\n"}),
        _layer({"apt.txt": "tree=2.2.1-1\n"}),
    ])

    lock = parse_build_manifest_files(
        registry_client.manifest_build_lock("reg.example.com/o/sut:t"))

    assert lock["apt"]["tree"] == "2.2.1-1"


def test_an_oversized_layer_is_never_downloaded(monkeypatch):
    """The whole point is not pulling the image. The manifest layers are a package list; an
    install layer is not, and fetching one to look inside would cost what this avoids."""
    big = _layer({"apt.txt": "tree=2.2.1-1\n"})
    fetched = _wire(monkeypatch, [big], sizes={
        "sha256:" + f"{0:064x}": registry_client._LOCK_LAYER_MAX_BYTES + 1})

    assert registry_client.manifest_build_lock("reg.example.com/o/sut:t") == {}
    assert fetched == [], "the size cap must be applied before the GET, not after"


def test_an_uncompressed_layer_is_read(monkeypatch):
    """Uncompressed layers are legal in the OCI spec, so gzip must not be assumed."""
    _wire(monkeypatch, [_layer({"apt.txt": "tree=2.2.1-1\n"}, compress=False)])

    lock = parse_build_manifest_files(
        registry_client.manifest_build_lock("reg.example.com/o/sut:t"))

    assert lock["apt"]["tree"] == "2.2.1-1"


def test_an_image_with_no_manifest_reads_as_unknown(monkeypatch):
    """``{}`` is "could not be read", and a caller must not take it for "installed nothing":
    that would make a rebuild install an empty set in place of the author's intent."""
    _wire(monkeypatch, [_layer({"bashrc": "x"}, prefix="etc")])

    assert registry_client.manifest_build_lock("reg.example.com/o/sut:t") == {}


def test_a_stray_file_beside_the_lock_is_not_returned(monkeypatch):
    """Only the files the lock is made of: anything else under that directory is somebody
    else's, and decoding it would put unbounded image content into a small, known record."""
    _wire(monkeypatch, [_layer({"apt.txt": "tree=2.2.1-1\n", "notes.md": "x" * 100})])

    texts = registry_client.manifest_build_lock("reg.example.com/o/sut:t")

    assert set(texts) == {"apt.txt"}


def test_an_unreachable_registry_reads_as_unknown(monkeypatch):
    monkeypatch.setattr(registry_client, "_manifest_json", lambda *a, **k: None)

    assert registry_client.manifest_build_lock("reg.example.com/o/sut:t") == {}


def test_a_layer_that_will_not_unpack_is_skipped(monkeypatch):
    """A record must never be a reason a campaign cannot run."""
    _wire(monkeypatch, [b"not a tar at all", _layer({"apt.txt": "tree=2.2.1-1\n"})])

    lock = parse_build_manifest_files(
        registry_client.manifest_build_lock("reg.example.com/o/sut:t"))

    assert lock["apt"]["tree"] == "2.2.1-1"


def test_the_hook_is_on_the_class_the_controller_actually_holds():
    """The bug this guards is invisible at runtime: put on the batch runner instead of the
    backend, the override is simply never reached -- the controller asks its backend, gets the
    base class's "cannot read one here", and the campaign silently keeps no lock, exactly as
    before the fix. Nothing raises, so only this asserts it.

    The runner is also the wrong place on its own terms: it exists per batch, and the lock is
    read once at the end of the campaign, when no runner is alive.
    """
    from robovast.execution.backends import ExecutionBackend
    from robovast.execution.cluster_execution.kubernetes_backend import (BatchJobRunner,
                                                                         KubernetesBackend)

    assert KubernetesBackend.read_build_lock is not ExecutionBackend.read_build_lock
    assert not hasattr(BatchJobRunner, "read_build_lock")


def test_both_registry_callers_share_one_credential_reader():
    """The digest resolution and the lock read must resolve credentials identically. A second
    copy would drift, and a drift here reads as an image with nothing to report rather than as
    a credential that could not be found."""
    from robovast.execution.cluster_execution import kubernetes_backend as kb

    assert callable(kb.registry_dockerconfig)
    assert callable(kb.registry_ca_file)
    # The runner keeps its method, but as a delegation rather than an implementation.
    import inspect
    body = inspect.getsource(kb.BatchJobRunner._registry_dockerconfig)
    assert "registry_dockerconfig(" in body and "b64decode" not in body


def _backend():
    """A real KubernetesBackend, not a stub. The point of these tests."""
    from robovast.execution.cluster_execution.kubernetes_backend import KubernetesBackend
    return KubernetesBackend(cluster_config=object(), namespace="default")


def test_the_backend_reads_the_lock_without_a_kubernetes_client():
    """The bug a stubbed test cannot see: the previous implementation resolved the pull Secret
    itself, through a client this class does not have. Every call raised, the broad `except`
    turned it into ``{}``, and ``{}`` is exactly "this image carries no lock" -- so the fix was
    indistinguishable from the absence it was written to remove.

    Asserted on a real instance, and on the attribute rather than the outcome, because a
    mocked client would hide precisely this.
    """
    backend = _backend()

    assert not hasattr(backend, "k8s_client"), (
        "read_build_lock must not depend on a client this class does not have")
    # Answers from the cache a runner fills, so it works with no cluster at all.
    assert backend.read_build_lock("reg.example.com/o/sut@sha256:a") == {}
    backend._build_lock_cache["reg.example.com/o/sut@sha256:a"] = {"apt": {"tree": "2.2.1-1"}}
    assert backend.read_build_lock(
        "reg.example.com/o/sut@sha256:a")["apt"]["tree"] == "2.2.1-1"


def test_the_cache_is_shared_with_every_runner_of_the_campaign():
    """One registry read per image per campaign, and -- more importantly -- a lock a runner
    read during the batch is still there when the controller asks after it."""
    from robovast.execution.cluster_execution import kubernetes_backend as kb
    import inspect

    backend = _backend()
    assert isinstance(backend._build_lock_cache, dict)
    # The batch runner accepts it, and run_batch hands it over.
    assert "build_lock_cache" in inspect.signature(kb.BatchJobRunner.for_batch).parameters
    assert "build_lock_cache=self._build_lock_cache" in inspect.getsource(kb.KubernetesBackend.run_batch)


def test_a_returned_lock_cannot_be_mutated_through_the_cache():
    """The caller writes what it gets into the campaign's record; handing out the cached dict
    would let one role's bookkeeping edit another's."""
    backend = _backend()
    backend._build_lock_cache["img"] = {"apt": {"tree": "1.0"}}

    got = backend.read_build_lock("img")
    got["apt"] = {}

    assert backend.read_build_lock("img")["apt"] == {"tree": "1.0"}
