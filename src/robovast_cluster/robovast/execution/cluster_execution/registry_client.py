# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Minimal read-only registry v2 client: does this image tag already exist?

Only one question is asked here, and it is the durable answer to "has this exact build
already been pushed?". The previous answer came from the build Job's status, which expires
with ``ttlSecondsAfterFinished`` (1 h) and is lost on a service restart — after which a
bit-identical image was rebuilt and re-pushed from scratch. The registry is the actual
source of truth and outlives both.

**Fails closed on purpose.** Any uncertainty — no credentials, an unreachable registry, an
unexpected status — is reported as "not present", which costs a redundant rebuild. The
opposite error is far worse: claiming an image exists when it does not leaves the campaign
pods in ``ImagePullBackOff`` with the build long finished, which reads as a broken cluster
rather than a cache bug. Every such case is logged at warning level rather than swallowed.

That collapse is right for *that* question and wrong for "is this image there to run?", so
the probe itself reports three states (:func:`manifest_state`) and ``manifest_exists`` is the
fail-closed view of it. A caller who must not confuse "could not ask" with "not there" asks
for the state.
"""

import base64
import hashlib
import json
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

#: Both manifest flavours plus their list/index forms — a registry may hold any of them and
#: answers 404 for an unrequested media type even when the tag exists.
_ACCEPT = ", ".join([
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
])

_TIMEOUT = 10


def split_image_ref(image_ref: str) -> "tuple[str, str, str]":
    """``host[:port]/path/name:tag`` → ``(host, repository, tag)``.

    The tag is split off the *last* colon after the final ``/`` so a registry port is not
    mistaken for a tag (``registry.local:5000/x/y`` has no tag).
    """
    if "/" not in image_ref:
        raise ValueError(f"not a registry-qualified image ref: {image_ref!r}")
    host, remainder = image_ref.split("/", 1)
    if ":" in remainder:
        repository, tag = remainder.rsplit(":", 1)
    else:
        repository, tag = remainder, "latest"
    return host, repository, tag


def credentials_for(dockerconfigjson: str, host: str) -> "Optional[tuple[str, str]]":
    """Pull ``(username, password)`` for *host* out of a dockerconfigjson blob."""
    try:
        auths = json.loads(dockerconfigjson).get("auths", {})
    except (ValueError, AttributeError):
        logger.warning("registry check: push secret is not valid dockerconfigjson")
        return None
    entry = auths.get(host)
    if entry is None:
        # Docker Hub is conventionally keyed by its legacy index URL rather than the host.
        for key, value in auths.items():
            if key.rstrip("/").endswith(host):
                entry = value
                break
    if entry is None:
        return None
    if entry.get("username") and entry.get("password"):
        return entry["username"], entry["password"]
    if entry.get("auth"):
        try:
            user, _, password = base64.b64decode(entry["auth"]).decode().partition(":")
            return user, password
        except (ValueError, UnicodeDecodeError):
            logger.warning("registry check: unreadable 'auth' entry for %s", host)
    return None


def _bearer_token(session, challenge: str, creds) -> "tuple[Optional[str], float]":
    """Satisfy a ``WWW-Authenticate: Bearer …`` challenge: ``(token, lifetime_seconds)``.

    The lifetime comes back with the token because only the response knows it, and the caller
    caches on it. ``(None, 0)`` when the challenge cannot be satisfied.
    """
    if not challenge.lower().startswith("bearer "):
        return None, 0.0
    params = {}
    for part in challenge[len("bearer "):].split(","):
        key, _, value = part.strip().partition("=")
        params[key.strip()] = value.strip().strip('"')
    realm = params.pop("realm", "")
    if not realm:
        return None, 0.0
    try:
        resp = session.get(realm, params=params,
                           auth=creds if creds else None, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None, 0.0
        payload = resp.json()
        token = payload.get("token") or payload.get("access_token")
        if not token:
            return None, 0.0
        expires_in = payload.get("expires_in")
        try:
            ttl = float(expires_in) if expires_in is not None else _TOKEN_DEFAULT_TTL_S
        except (TypeError, ValueError):
            ttl = _TOKEN_DEFAULT_TTL_S
        return token, ttl
    except Exception as e:  # noqa: BLE001 - any failure here means "cannot confirm"
        logger.warning("registry check: token request to %s failed: %s", realm, e)
        return None, 0.0


#: :func:`manifest_state` verdicts. ``UNKNOWN`` is not a synonym for ``ABSENT``: it means the
#: registry could not be asked, which different callers must answer differently.
PRESENT, ABSENT, UNKNOWN = "present", "absent", "unknown"


def manifest_exists(image_ref: str, *, dockerconfigjson: str = "",
                    insecure: bool = False, ca_path: str = "") -> bool:
    """True only when *image_ref*'s manifest is definitely present in the registry.

    The **fail-closed** view of :func:`manifest_state`, for the caller that asks "should I
    build this?": uncertainty answers "not present" and costs a redundant rebuild. See the
    module docstring for why that is the right trade *there*.

    It is the wrong trade for "is this image available to run?" — reporting an unreachable
    registry as an unbuilt image sends the caller off to rebuild something that already
    exists, and once cost a real investigation. That caller asks :func:`manifest_state` and
    handles ``UNKNOWN`` itself.
    """
    return manifest_state(image_ref, dockerconfigjson=dockerconfigjson,
                          insecure=insecure, ca_path=ca_path) == PRESENT


#: Bearer tokens already obtained, keyed by what they are valid FOR, with the epoch each
#: stops being usable. See :func:`_token_key` for why the key is shaped as it is.
_TOKENS: "dict[tuple, tuple[str, float]]" = {}
_TOKENS_LOCK = threading.Lock()

#: Trimmed off every token's advertised lifetime, so one is never presented in the second it
#: expires. Registries commonly issue 60-second tokens, so this stays small.
_TOKEN_SAFETY_S = 5

#: Assumed lifetime when the token response does not carry ``expires_in``. The registry v2
#: spec's own default, and short enough that guessing it wrong costs one extra dance.
_TOKEN_DEFAULT_TTL_S = 60


def _token_key(host: str, path: str, creds) -> tuple:
    """What a cached token is valid for: the host, the repository, and whose credential got it.

    The repository is in the key because a registry token is scoped to one: reusing repo A's
    token on repo B earns a 401, which is survivable (the dance simply reruns) but pointless.
    It is derived from *path* rather than passed in, because this module builds only two path
    shapes -- ``<repo>/manifests/<ref>`` and ``<repo>/blobs/<digest>`` -- and splitting on
    those is exact. A third shape must extend this.

    The credential is in the key as a DIGEST, never the password itself: one controller drives
    campaigns whose namespaces may hold different pull secrets, and a token fetched with one
    must not be handed to another. Hashing it keeps a credential out of a process-lifetime dict
    that nothing redacts.
    """
    repository = path.split("/manifests/")[0].split("/blobs/")[0]
    if creds:
        who = hashlib.sha256(f"{creds[0]}:{creds[1]}".encode()).hexdigest()[:16]
    else:
        who = "anonymous"
    return (host, repository, who)


def _cached_token(key: tuple) -> "Optional[str]":
    """A token for *key* that is still good, or ``None``."""
    with _TOKENS_LOCK:
        entry = _TOKENS.get(key)
        if entry is None:
            return None
        token, expires_at = entry
        if time.time() >= expires_at:
            del _TOKENS[key]
            return None
        return token


def _store_token(key: tuple, token: str, ttl: float) -> None:
    """Remember *token* for *key*, unless it has no usable life left.

    A lifetime at or inside the safety margin is not stored at all. The caller still uses the
    token it holds for the retry it is about to make, but a registry saying ``expires_in`` is
    seconds means the token is not reusable, and keeping it anyway only guarantees the next
    request presents something stale.
    """
    usable = ttl - _TOKEN_SAFETY_S
    if usable <= 0:
        return
    with _TOKENS_LOCK:
        _TOKENS[key] = (token, time.time() + usable)


def _forget_token(key: tuple) -> None:
    """Drop *key*'s token: it was presented and refused, so it is not usable any more."""
    with _TOKENS_LOCK:
        _TOKENS.pop(key, None)


def _registry_request(host: str, path: str, *, method: str = "HEAD",
                      dockerconfigjson: str = "", insecure: bool = False,
                      ca_path: str = "", accept: str = _ACCEPT):
    """One authenticated v2 request against *host*, or ``None``.

    Speaks just enough of the v2 API: the request, retried once with a Bearer token when
    the registry issues an auth challenge. ``None`` means the registry could not be asked
    at all -- credentials it would not accept, a host that did not answer -- as distinct
    from a response that says 404.

    Every read here goes through this one function so the credential handling has a single
    home: the challenge dance is the part that is easy to get subtly wrong, and a second
    copy of it would be a second place to fix.
    """
    import requests

    scheme = "http" if insecure else "https"
    url = f"{scheme}://{host}/v2/{path}"
    verify: "bool | str" = True
    if insecure:
        verify = False
    elif ca_path:
        verify = ca_path

    creds = credentials_for(dockerconfigjson, host) if dockerconfigjson else None
    headers = {"Accept": accept}
    key = _token_key(host, path, creds)

    try:
        with requests.Session() as session:
            # Dispatched by name rather than through ``session.request``: the verb is a
            # literal at every call site, and the named methods are what a Session is
            # substituted for in tests.
            call = getattr(session, method.lower())
            # A token already held for this repository is presented up front, so a walk of
            # several requests costs one challenge instead of one per request. Reading an
            # image's labels is three requests through an index, and pinning a campaign's
            # refs is one per ref -- which was a dozen round trips and half a dozen separate
            # token requests within a second or two, against a token endpoint that is
            # entitled to rate-limit exactly that.
            token = _cached_token(key)
            if token:
                resp = call(url, verify=verify, timeout=_TIMEOUT,
                            headers={**headers, "Authorization": f"Bearer {token}"})
                if resp.status_code != 401:
                    return resp
                # Presented and refused: revoked, or expiring inside our safety margin.
                # Drop it and fall through to earn a fresh one rather than failing, so a
                # stale cache is never worse than no cache.
                _forget_token(key)
                token = None
            resp = call(url, headers=headers, verify=verify,
                        auth=creds if creds else None, timeout=_TIMEOUT)
            if resp.status_code == 401:
                token, ttl = _bearer_token(session,
                                           resp.headers.get("WWW-Authenticate", ""), creds)
                if token is None:
                    logger.warning(
                        "registry check: %s needs authentication that could not be "
                        "satisfied", host)
                    return None
                _store_token(key, token, ttl)
                resp = call(url, verify=verify, timeout=_TIMEOUT,
                            headers={**headers, "Authorization": f"Bearer {token}"})
            return resp
    except Exception as e:  # noqa: BLE001 - never let a cache probe break a build
        logger.warning("registry check: could not reach %s (%s)", host, e)
        return None


def _head_manifest(image_ref: str, *, dockerconfigjson: str = "",
                   insecure: bool = False, ca_path: str = ""):
    """The registry's ``HEAD`` response for *image_ref*'s manifest, or ``None``.

    Shared by the two questions worth asking of a manifest without downloading it: is it
    there (:func:`manifest_state`), and what does the tag currently point at
    (:func:`manifest_digest`). One implementation because they differ only in which part
    of the same response they read.
    """
    try:
        host, repository, tag = split_image_ref(image_ref)
    except ValueError as e:
        logger.warning("registry check: %s", e)
        return None
    return _registry_request(host, f"{repository}/manifests/{tag}", method="HEAD",
                             dockerconfigjson=dockerconfigjson, insecure=insecure,
                             ca_path=ca_path)


def manifest_state(image_ref: str, *, dockerconfigjson: str = "",
                   insecure: bool = False, ca_path: str = "") -> str:
    """``PRESENT`` / ``ABSENT`` / ``UNKNOWN`` for *image_ref*'s manifest.

    Only a 200 and a 404 are answers; everything else — no usable credentials, an
    unreachable host, a status neither of those — is ``UNKNOWN``, because the registry
    did not say.
    """
    resp = _head_manifest(image_ref, dockerconfigjson=dockerconfigjson,
                          insecure=insecure, ca_path=ca_path)
    if resp is None:
        return UNKNOWN
    if resp.status_code == 200:
        return PRESENT
    if resp.status_code == 404:
        return ABSENT
    logger.warning(
        "registry check: unexpected status %s for %s; the registry did not say "
        "whether the image is there", resp.status_code, image_ref)
    return UNKNOWN


def manifest_digest(image_ref: str, *, dockerconfigjson: str = "",
                    insecure: bool = False, ca_path: str = "") -> str:
    """What *image_ref* points at **right now**, as ``repo@sha256:…``; ``""`` if unknown.

    The registry answers this in the ``Docker-Content-Digest`` header of a ``HEAD``, so it
    costs one round trip and no layer bytes.

    A ref that already carries a digest is returned unchanged without asking anyone: it
    already names the bytes, and re-resolving it could only introduce a difference.

    Empty on every uncertainty -- an unreachable registry, a tag that is not there, a
    registry that omits the header. The caller keeps the tag it had, which is what it
    would have used anyway; a digest is an improvement to make when it is available, never
    a reason to refuse to run.
    """
    if "@sha256:" in image_ref:
        return image_ref
    resp = _head_manifest(image_ref, dockerconfigjson=dockerconfigjson,
                          insecure=insecure, ca_path=ca_path)
    if resp is None or resp.status_code != 200:
        return ""
    digest = (resp.headers.get("Docker-Content-Digest") or "").strip()
    if not digest.startswith("sha256:"):
        return ""
    try:
        host, repository, _ = split_image_ref(image_ref)
    except ValueError:
        return ""
    return f"{host}/{repository}@{digest}"


#: Where an image records when it was built. The OCI label first: robovast stamps it from
#: the build's own clock (``container/image_stamp.sh``), whereas the config's ``created``
#: is written by the builder and is deliberately zeroed by reproducible builds -- so the
#: label is the one that answers "how old is this?" when both are present.
_CREATED_LABEL = "org.opencontainers.image.created"

#: A zeroed ``created``, as a reproducible build leaves it. Not a date anyone wants shown.
_EPOCH_ZERO_PREFIXES = ("1970-01-01", "0001-01-01")


def manifest_created(image_ref: str, *, dockerconfigjson: str = "",
                     insecure: bool = False, ca_path: str = "") -> str:
    """When *image_ref* was built (RFC 3339), or ``""`` when it cannot be read.

    The question :func:`manifest_digest` cannot answer: a digest identifies bytes, and two
    digests differing tells an operator *that* the published image is not the running one
    but not whether it is newer or by how much. A date reads on its own.

    Costs two small GETs on top of the digest probe -- the manifest, then the image config
    blob it names -- and one more when the tag resolves to a multi-arch index. Manifests
    and configs are a few KB of JSON; no layer is ever fetched.

    ``""`` on every uncertainty, exactly as :func:`manifest_digest` returns ``""``: an
    unreachable registry, an image built without the stamp, a config that will not parse.
    This is an addition to what a caller already shows, never a new way for the rest of it
    to come back empty -- so it must not raise and must not manufacture a date.
    """
    # `or {}` collapses the two cases deliberately: this reader's own contract is that `""`
    # covers an unreachable registry and an unstamped image alike, because a date is an
    # addition to what the caller already shows rather than a verdict about the image.
    config = _image_config(image_ref, dockerconfigjson=dockerconfigjson,
                           insecure=insecure, ca_path=ca_path) or {}
    labels = ((config.get("config") or {}).get("Labels") or {})
    created = (labels.get(_CREATED_LABEL) or config.get("created") or "").strip()
    if created.startswith(_EPOCH_ZERO_PREFIXES):
        return ""
    return created


def manifest_labels(image_ref: str, *, dockerconfigjson: str = "",
                    insecure: bool = False, ca_path: str = "") -> "Optional[dict]":
    """Every label *image_ref* carries per the registry, or ``None`` if it could not be read.

    The same walk :func:`manifest_created` does -- manifest, index child if there is one, then
    the image config blob -- returning the whole label map rather than one date out of it. Both
    answer "what does this image say about itself", and asking the registry twice for the same
    config blob to read two labels out of it would be silly.

    Costs the same two small GETs (three through an index), and fetches no layer.

    Three states in two return values, unlike :func:`manifest_created` above: ``None`` is "the
    registry could not be asked" -- unreachable, a ref this deployment holds no credential for,
    a config that will not parse -- and ``{}`` is "asked, and it carries no labels". A caller
    deciding whether an image may be RUN needs those apart, because only the second is a fact
    about the image. See :func:`manifest_state` for the same rule spelled as three values.
    """
    config = _image_config(image_ref, dockerconfigjson=dockerconfigjson,
                           insecure=insecure, ca_path=ca_path)
    if config is None:
        return None
    labels = (config.get("config") or {}).get("Labels") or {}
    return {str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {}


def _image_config(image_ref: str, *, dockerconfigjson: str = "",
                  insecure: bool = False, ca_path: str = "") -> "Optional[dict]":
    """*image_ref*'s image config blob, parsed, or ``None`` when it could not be read.

    Shared by the two readers above so the manifest -> index child -> config blob walk exists
    once.

    ``None`` rather than ``{}`` on uncertainty, and the distinction is the point: a config that
    was fetched and parsed lets a caller say the image carries no such label, while one that was
    never fetched lets it say only that nothing was established. Collapsing the two is what had
    an unauthenticated registry read reported as "this image has no compat label, rebuild it" --
    advice that cannot work, because the image was never the problem. The module docstring
    states the rule for :func:`manifest_state`; this applies it one level down.

    Never raises.
    """
    try:
        host, repository, _ = split_image_ref(image_ref)
    except ValueError as e:
        logger.warning("registry check: %s", e)
        return None
    manifest = _manifest_json(image_ref, dockerconfigjson=dockerconfigjson,
                              insecure=insecure, ca_path=ca_path)
    if manifest is None:
        return None
    if "manifests" in manifest:
        # An index: the tag names a set of per-platform images, none of which is the
        # config. Any of them answers -- they are pushed together -- so this takes the
        # first real entry rather than matching a platform the caller has not named.
        child = _index_child(manifest)
        if not child:
            return None
        manifest = _manifest_json(image_ref, digest=child,
                                  dockerconfigjson=dockerconfigjson,
                                  insecure=insecure, ca_path=ca_path)
        if manifest is None:
            return None
    config_digest = (manifest.get("config") or {}).get("digest") or ""
    if not config_digest.startswith("sha256:"):
        return None
    resp = _registry_request(host, f"{repository}/blobs/{config_digest}", method="GET",
                             dockerconfigjson=dockerconfigjson, insecure=insecure,
                             ca_path=ca_path, accept="application/json")
    if resp is None or resp.status_code != 200:
        return None
    try:
        parsed = resp.json()
    except ValueError:
        logger.warning("registry check: image config for %s is not JSON", image_ref)
        return None
    return parsed if isinstance(parsed, dict) else None


def _manifest_json(image_ref: str, *, digest: str = "", dockerconfigjson: str = "",
                   insecure: bool = False, ca_path: str = "") -> "Optional[dict]":
    """The parsed manifest for *image_ref*'s tag, or for *digest* when given."""
    try:
        host, repository, tag = split_image_ref(image_ref)
    except ValueError:
        return None
    resp = _registry_request(host, f"{repository}/manifests/{digest or tag}", method="GET",
                             dockerconfigjson=dockerconfigjson, insecure=insecure,
                             ca_path=ca_path)
    if resp is None or resp.status_code != 200:
        return None
    try:
        parsed = resp.json()
    except ValueError:
        logger.warning("registry check: manifest for %s is not JSON", image_ref)
        return None
    return parsed if isinstance(parsed, dict) else None


def _index_child(index: dict) -> str:
    """The digest of an image manifest inside a multi-arch *index*, or ``""``.

    Attestation entries are skipped: buildx pushes them alongside the images with an
    ``unknown`` platform, and their config is not an image config, so picking one would
    read as an image with no build date rather than as the wrong entry.
    """
    for entry in index.get("manifests") or []:
        platform = entry.get("platform") or {}
        if platform.get("architecture") in ("unknown", None) and platform:
            continue
        digest = entry.get("digest") or ""
        if digest.startswith("sha256:"):
            return digest
    return ""
