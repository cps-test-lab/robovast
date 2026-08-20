# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""In-cluster build caching: the registry layer cache and the manifest existence probe.

A fresh BuildKit pod has no local cache, so the registry cache ref is the only layer reuse
available in-cluster; and the manifest probe is what makes a cache hit outlive the build
Job's 1 h TTL. Both are asserted here because a mistake in either is silent — the build
simply gets slow again, or (worse, see ``registry_client``) claims a hit for an image that
was never pushed.
"""

import json

import pytest

from robovast.execution.cluster_execution.cluster_image_build import (build_job_manifest,
                                                                      cache_image_ref,
                                                                      concrete_image_ref)
from robovast.service.image_build import BuildSpec, cache_scope
from robovast.execution.cluster_execution.registry_client import (credentials_for, manifest_exists,
                                                                  split_image_ref)


def _buildctl(**over):
    kwargs = {"build_id": "imgbuild-x-abc", "image_ref": "reg.local:5000/x:abc",
              "campaign_label": "imgbuild-x-abc", "init_env": [],
              "push_secret_name": "push", "namespace": "ns"}
    kwargs.update(over)
    manifest = build_job_manifest(**kwargs)
    return manifest['spec']['template']['spec']['containers'][0]['command'][-1]


# ---------------------------------------------------------------------------
# cache ref naming
# ---------------------------------------------------------------------------

def test_cache_ref_is_not_hash_qualified():
    """The whole point is importing layers built for a *different* hash."""
    a = concrete_image_ref("reg.local:5000/rv", "sim", "aaaaaaaaaaaa")
    b = concrete_image_ref("reg.local:5000/rv", "sim", "bbbbbbbbbbbb")
    assert a != b
    cache = cache_image_ref("reg.local:5000/rv", "sim", "scope00")
    assert cache == "reg.local:5000/rv/sim-scope00:buildcache"
    assert "aaaaaaaaaaaa" not in cache


def test_cache_ref_folds_tag_version_like_the_image_ref():
    assert cache_image_ref("reg/rv", "sim:v2", "s0") == "reg/rv/sim-v2-s0:buildcache"


# ---------------------------------------------------------------------------
# cache SCOPE — which builds are allowed to share a cache ref
#
# The tag is the container's name, so it is `sut`/`simulation`/`scenario` for nearly every
# project. Keyed on that alone, every project in a deployment exported ``mode=max`` to the
# same three tags and evicted each other's layers in place. These four tests fix the two
# boundaries that has to sit between: iterations of one project must share, unrelated
# projects must not.
# ---------------------------------------------------------------------------

_BASE = "reg/rv/robovast@sha256:2edbf546"


def _sut(wheel="ma_edge_sensors-0.1.24-py3-none-any.whl", apt=("ros-jazzy-cv-bridge",)):
    return BuildSpec(tag="sut", system_packages=list(apt), python_packages=[
        ["torch==2.5.1", "SAM-2 @ git+https://github.com/facebookresearch/sam2@2b90b9f5"],
        [f"./plugins/{wheel}", "scipy"]])


def test_a_wheel_version_bump_keeps_the_cache_scope():
    """The case the whole scope exists to survive.

    A project bumps its own wheel on nearly every iteration -- pip treats a same-version
    wheel as already satisfied, so the bump is not optional. If that moved the namespace,
    every iteration would start from an empty cache: the collision this replaced, one
    level up and harder to see.
    """
    assert (cache_scope(_sut(), _BASE)
            == cache_scope(_sut("ma_edge_sensors-0.1.25-py3-none-any.whl"), _BASE))


def test_an_unrelated_project_named_sut_gets_its_own_scope():
    """The collision itself. Both are called `sut`; neither may evict the other."""
    other = BuildSpec(tag="sut", system_packages=["ros-jazzy-cv-bridge"],
                      python_packages=["numpy"])
    assert cache_scope(_sut(), _BASE) != cache_scope(other, _BASE)


def test_apt_and_base_changes_retire_the_scope():
    """Both sit *above* every pip layer, so layers cached under them cannot be reused."""
    assert cache_scope(_sut(), _BASE) != cache_scope(_sut(apt=("ros-jazzy-tf2-ros",)), _BASE)
    assert cache_scope(_sut(), _BASE) != cache_scope(_sut(), _BASE + "-other")


def test_group_boundaries_and_order_are_part_of_the_scope():
    """Same specs in one pass and in two render different layers, so different chains."""
    one = BuildSpec(tag="sut", python_packages=["a", "b"])
    two = BuildSpec(tag="sut", python_packages=[["a"], ["b"]])
    flipped = BuildSpec(tag="sut", python_packages=[["b"], ["a"]])
    assert cache_scope(one, _BASE) != cache_scope(two, _BASE)
    assert cache_scope(two, _BASE) != cache_scope(flipped, _BASE)


# ---------------------------------------------------------------------------
# buildctl invocation
# ---------------------------------------------------------------------------

def test_no_cache_flags_without_a_cache_ref():
    cmd = _buildctl()
    assert "--import-cache" not in cmd
    assert "--export-cache" not in cmd


def test_cache_import_and_export_use_mode_max():
    cmd = _buildctl(cache_ref="reg.local:5000/x:buildcache")
    assert "--import-cache type=registry,ref=reg.local:5000/x:buildcache" in cmd
    # mode=max exports intermediate layers too — without it only the final layer is
    # reusable, which is worthless for a chain of per-entry pip installs.
    assert "mode=max" in cmd
    # A failed cache *export* must not fail a build whose image is already pushed.
    assert "ignore-error=true" in cmd


def test_insecure_applies_to_cache_refs_not_only_the_output():
    """The cache refs address the same registry; missing the flag fails them on TLS."""
    cmd = _buildctl(cache_ref="reg.local:5000/x:buildcache", insecure=True)
    assert cmd.count("registry.insecure=true") == 3   # output + import + export


def test_ca_mount_suppresses_insecure_everywhere():
    """A mounted CA makes the registry properly trusted, so nothing is skipped."""
    cmd = _buildctl(cache_ref="reg.local:5000/x:buildcache", insecure=True,
                    ca_configmap_name="registry-ca")
    assert "registry.insecure=true" not in cmd
    assert "--import-cache" in cmd


# ---------------------------------------------------------------------------
# ref parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ref,expected", [
    ("reg.local:5000/rv/sim:abc123", ("reg.local:5000", "rv/sim", "abc123")),
    ("ghcr.io/org/sim:abc", ("ghcr.io", "org/sim", "abc")),
    # A registry port must not be mistaken for a tag.
    ("registry.local:5000/x/y", ("registry.local:5000", "x/y", "latest")),
])
def test_split_image_ref(ref, expected):
    assert split_image_ref(ref) == expected


def test_split_image_ref_rejects_unqualified():
    with pytest.raises(ValueError):
        split_image_ref("bare-name:tag")


def test_credentials_from_auth_blob():
    import base64
    auth = base64.b64encode(b"user:pw").decode()
    cfg = json.dumps({"auths": {"reg.local:5000": {"auth": auth}}})
    assert credentials_for(cfg, "reg.local:5000") == ("user", "pw")


def test_credentials_prefer_explicit_fields():
    cfg = json.dumps({"auths": {"h": {"username": "u", "password": "p"}}})
    assert credentials_for(cfg, "h") == ("u", "p")


def test_credentials_missing_host_is_none():
    assert credentials_for(json.dumps({"auths": {}}), "h") is None


def test_credentials_garbage_is_none():
    assert credentials_for("not json", "h") is None


# ---------------------------------------------------------------------------
# manifest_exists — must fail closed
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


class _Session:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def head(self, url, **kw):
        self.calls.append(url)
        return self._responses.pop(0)


def _patch_session(monkeypatch, session):
    import requests
    monkeypatch.setattr(requests, "Session", lambda: session)


def test_manifest_present(monkeypatch):
    _patch_session(monkeypatch, _Session([_Resp(200)]))
    assert manifest_exists("reg.local:5000/x:abc") is True


def test_manifest_absent(monkeypatch):
    _patch_session(monkeypatch, _Session([_Resp(404)]))
    assert manifest_exists("reg.local:5000/x:abc") is False


def test_unexpected_status_reports_absent(monkeypatch):
    """A 500 must never be read as a cache hit — that yields ImagePullBackOff."""
    _patch_session(monkeypatch, _Session([_Resp(500)]))
    assert manifest_exists("reg.local:5000/x:abc") is False


def test_unreachable_registry_reports_absent(monkeypatch):
    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def head(self, *a, **kw):
            raise OSError("connection refused")

    _patch_session(monkeypatch, _Boom())
    assert manifest_exists("reg.local:5000/x:abc") is False


def test_unsatisfiable_auth_challenge_reports_absent(monkeypatch):
    _patch_session(monkeypatch, _Session([_Resp(401, {"WWW-Authenticate": "Basic"})]))
    assert manifest_exists("reg.local:5000/x:abc") is False


def test_bad_ref_reports_absent(monkeypatch):
    assert manifest_exists("bare-name") is False


def test_insecure_uses_http_and_skips_verify(monkeypatch):
    seen = {}

    class _S(_Session):
        def head(self, url, **kw):
            seen["url"] = url
            seen["verify"] = kw.get("verify")
            return _Resp(200)

    _patch_session(monkeypatch, _S([]))
    assert manifest_exists("reg.local:5000/x:abc", insecure=True) is True
    assert seen["url"].startswith("http://")
    assert seen["verify"] is False


def test_ca_path_is_passed_as_verify(monkeypatch):
    seen = {}

    class _S(_Session):
        def head(self, url, **kw):
            seen["verify"] = kw.get("verify")
            return _Resp(200)

    _patch_session(monkeypatch, _S([]))
    assert manifest_exists("reg.local:5000/x:abc", ca_path="/certs/ca.pem") is True
    assert seen["verify"] == "/certs/ca.pem"


# ---------------------------------------------------------------------------
# the pod's own credentials
# ---------------------------------------------------------------------------

def _pod_spec(**over):
    kwargs = {"build_id": "imgbuild-x-abc", "image_ref": "reg.local:5000/x:abc",
              "campaign_label": "imgbuild-x-abc", "init_env": [],
              "push_secret_name": "push", "namespace": "ns"}
    kwargs.update(over)
    return build_job_manifest(**kwargs)['spec']['template']['spec']


def test_the_build_pod_carries_a_pull_secret_when_there_is_one():
    """The push Secret authenticates the *output*; the pod still has to pull its own
    images. The init container is the private-registry sidecar, so without this the Job
    sat in ImagePullBackOff -- and, since Kubernetes leaves such a Job active, nothing
    ever failed and the wait never returned."""
    spec = _pod_spec(pull_secret_name="rv-registry")
    assert spec['imagePullSecrets'] == [{'name': 'rv-registry'}]


def test_no_pull_secret_key_at_all_when_none_is_configured():
    """A public registry needs none, and naming a Secret that does not exist is itself
    enough to keep the pod from starting."""
    assert 'imagePullSecrets' not in _pod_spec()
    assert 'imagePullSecrets' not in _pod_spec(pull_secret_name="")


def test_the_pull_secret_covers_both_containers_of_the_pod():
    """One pod-level entry rather than a per-container guess: the sidecar and BuildKit come
    from different registries and either can be the one that cannot be pulled."""
    spec = _pod_spec(pull_secret_name="rv-registry")
    assert [c['name'] for c in spec['initContainers']] == ['context-fetch']
    assert [c['name'] for c in spec['containers']] == ['buildkit']
    for container in spec['initContainers'] + spec['containers']:
        assert 'imagePullSecrets' not in container


# ---------------------------------------------------------------------------
# the git token a private python_packages spec installs with
# ---------------------------------------------------------------------------

def test_a_git_token_reaches_the_build_as_a_secret():
    """A BuildKit secret, not a build arg: it is readable only by the RUN that installs, so it
    is in no layer and no image history -- which a build arg would be, permanently."""
    assert "--secret id=git_token,src=/var/run/secrets/robovast-git/token" in _buildctl(
        git_secret_name="robovast-git-credentials")


def test_the_secret_is_mounted_read_only_from_the_service_s_own_secret():
    pod = _pod_spec(git_secret_name="robovast-git-credentials")
    volume = next(v for v in pod["volumes"] if v["name"] == "git-credentials")
    assert volume["secret"]["secretName"] == "robovast-git-credentials"
    build = pod["containers"][0]
    mount = next(m for m in build["volumeMounts"] if m["name"] == "git-credentials")
    assert mount["mountPath"] == "/var/run/secrets/robovast-git"
    assert mount["readOnly"] is True


def test_no_token_configured_builds_without_one():
    """`--secret` naming a Secret that does not exist keeps the pod from starting, and a
    deployment with no private spec must not need a credential to build at all. The private
    case has already failed by here, at resolution, where the message names the fix."""
    assert "--secret" not in _buildctl()
    assert all(v["name"] != "git-credentials" for v in _pod_spec()["volumes"])


def test_the_build_user_can_actually_read_the_token():
    """Asserted as a property of the pod, not as a literal mode, because the literal is what
    went wrong: mounted 0400 -- copied from the service pod, which reads it as root -- the file
    is owned by root and this container runs as uid 1000, so the build died with `failed to
    solve: open /var/run/secrets/robovast-git/token: permission denied`. A test pinning 0400
    passed while the build could not read a byte."""
    pod = _pod_spec(git_secret_name="robovast-git-credentials")
    build = pod["containers"][0]
    uid = build["securityContext"]["runAsUser"]
    volume = next(v for v in pod["volumes"] if v["name"] == "git-credentials")
    mode = volume["secret"].get("defaultMode", 0o644)  # Kubernetes' default when unset
    assert uid != 0, "this test only means something while the build runs unprivileged"
    assert mode & 0o004, (
        f"mode {mode:#o} is not world-readable, and the file is root-owned: uid {uid} "
        f"cannot read it")
