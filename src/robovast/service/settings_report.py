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

"""Reporting this service's effective configuration — the read-back half of ``.env``.

:mod:`robovast.common.env_file` loads a ``.env`` on the operator's machine;
``vast cluster setup`` / ``vast service upgrade`` then bake those values into the pod as
container ``env`` and as Secrets pulled in via ``envFrom``. This module answers the
question nothing answered before: *what is this service actually running with?*

**The environment is the list, not** :data:`KNOWN`. Everything a ``.env`` set arrives here
as an environment variable, so enumerating ``ROBOVAST_*`` out of :data:`os.environ` is
exactly — and always — the set of settings in force. :data:`KNOWN` says only how to
*present* what the environment already reports, plus a courtesy row for a setting that is
expected and absent. An earlier design inverted this, hand-listing every setting and looking
each one up; that list is wrong the first time somebody adds a setting, and no amount of
guard-testing makes a parallel list true.

**Classification fails safe.** A credential cannot be recognised by its name:
``ROBOVAST_SHARE_URL`` is one (the Nextcloud provider parses its last path segment as the
share token) and nothing in the name says so. So showing a value requires an explicit
:class:`Sensitivity`; a key nobody has classified is reported as *set* with the value
withheld. That is harmless, still tells an operator the setting is in force, and cannot leak
something added later.

**Nothing here reaches into another distribution.** Defaults are imported from the constant
the reading code already uses, never restated — but only where that constant is in this
distribution. The cluster lane's settings live in ``robovast-cluster``, which a local install
does not have, so they are described without a default rather than by an optional import that
would silently report "no default" on exactly the deployment where they matter.
"""

import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from robovast.common.execution import DEFAULT_IMAGE_PROJECT, FLOATING_IMAGE_TAG
from robovast.execution.notify import DEFAULT_SERVER as DEFAULT_NTFY_SERVER
from robovast.execution.share_providers.sftp import DEFAULT_SFTP_PORT

from .scene_cache import DEFAULT_MAX_CACHE_BYTES

#: Only these are reported. A local ``vast serve`` inherits the operator's whole shell, and
#: enumerating that would put unrelated environment — including other tools' credentials —
#: into an HTTP response. The prefix is the boundary of what is ours to report.
PREFIX = "ROBOVAST_"

#: Where the deployment mounts the GitHub token, read-only and deliberately never as an
#: environment variable so the processes the service launches do not inherit it. It is the
#: one setting whose presence cannot be seen in ``os.environ``.
GIT_TOKEN_KEY = "ROBOVAST_GIT_TOKEN"
GIT_TOKEN_MOUNT = "/var/run/secrets/robovast-git/token"


class Sensitivity(Enum):
    """Whether a setting's *value* may be shown, and to whom.

    A static property of the setting. Whether a given caller actually receives the value is
    a second, per-request question — see :func:`describe` — because :attr:`HOST_PATH` is not
    "hidden", it is "hidden from a remote caller".
    """

    #: Shown to any authenticated caller.
    PUBLIC = "public"
    #: A credential. The value never leaves this process, in any form: no prefix, no mask,
    #: no reveal. RoboVAST has no per-route authorization (see ``service.auth``), so
    #: anything this response carries is available to every logged-in caller.
    SECRET = "secret"
    #: Registry endpoints, prefixes and fully-qualified refs, which by a standing rule do
    #: not cross the client interface (``cluster_config.base_config.RegistryConfig``,
    #: ``VersionInfo.build_unavailable``). Set/not-set only.
    SERVER_ONLY = "server_only"
    #: A path on the service's host. Shown only to a loopback caller — the same rule
    #: ``/version`` applies to ``results_root`` / ``sources_root``, so the two admin
    #: surfaces do not disagree about whether host paths are publishable.
    HOST_PATH = "host_path"
    #: Process plumbing rather than configuration: how a container was invoked, what the
    #: build stamped in. Not reported at all — an operator did not set it and cannot.
    INTERNAL = "internal"


#: Reason a caller received no value, when one is set. ``"unclassified"`` is the fail-safe:
#: a ``ROBOVAST_*`` key absent from :data:`KNOWN`.
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Known:
    """How to present one setting. **Not** the definition that it exists."""

    group: str
    description: str
    sensitivity: Sensitivity = Sensitivity.PUBLIC
    #: Imported from the constant the reading code uses; ``None`` where there is no default,
    #: or where the constant lives in a distribution this one must not import.
    default: Optional[str] = None


_IMAGES = "Container images"
_CLUSTER = "Cluster lane"
_REGISTRY = "Experiment image registry"
_SHARE = "Result share"
_NOTIFY = "Notifications"
_STORAGE = "Storage and caches"
_ACCESS = "Access"

KNOWN: dict[str, Known] = {
    # -- access ---------------------------------------------------------------
    "ROBOVAST_AUTH_TOKEN": Known(
        _ACCESS, "The shared secret every client authenticates with.",
        Sensitivity.SECRET),
    GIT_TOKEN_KEY: Known(
        _ACCESS, "Token for installing a variation plugin from a private git repository.",
        Sensitivity.SECRET),

    # -- image family ---------------------------------------------------------
    "ROBOVAST_PROJECT": Known(
        _IMAGES, "Registry namespace RoboVAST's own four images are pulled from.",
        default=DEFAULT_IMAGE_PROJECT),
    "ROBOVAST_PROJECT_TAG": Known(
        _IMAGES, "Tag the image family is pulled at; the default floats.",
        default=FLOATING_IMAGE_TAG),

    # -- cluster lane ---------------------------------------------------------
    # No defaults: these are read in `robovast-cluster`, which this distribution does not
    # import. Stating a literal here would be the restatement this module exists to avoid.
    "ROBOVAST_NAMESPACE": Known(
        _CLUSTER, "Kubernetes namespace this service and its campaign Jobs run in."),
    "ROBOVAST_KUBE_CONTEXT": Known(
        _CLUSTER, "Kubeconfig context recorded at deploy, for per-cluster resource lists."),
    "ROBOVAST_CLUSTER_CONFIG_NAME": Known(
        _CLUSTER, "Cluster flavor this service was set up with."),
    "ROBOVAST_CLUSTER_CONFIG_KWARGS": Known(
        _CLUSTER, "JSON options the cluster flavor was set up with."),
    "ROBOVAST_KUBE_CONNECT_TIMEOUT": Known(
        _CLUSTER, "Seconds before an unreachable cluster gives up connecting."),
    "ROBOVAST_JOB_NODE_LABELS": Known(
        _CLUSTER, "JSON node labels restricting where campaign Jobs are scheduled."),
    "ROBOVAST_NODE_CALIBRATION": Known(
        _CLUSTER, "Whether per-node capacity is calibrated rather than assumed."),
    "ROBOVAST_NODE_HEADROOM_CPU": Known(
        _CLUSTER, "CPU held back on each node when placing campaign Jobs."),
    "ROBOVAST_NODE_HEADROOM_MEMORY": Known(
        _CLUSTER, "Memory held back on each node when placing campaign Jobs."),
    "ROBOVAST_EXTRA_HOST_ALIASES": Known(
        _CLUSTER, "Extra host=ip entries injected into pods' /etc/hosts."),
    "ROBOVAST_PUBLIC_URL": Known(
        _CLUSTER, "The origin this service declares to its clients."),

    # -- experiment image registry --------------------------------------------
    "ROBOVAST_REGISTRY_SERVER": Known(
        _REGISTRY, "Host the external pull credential below is for.",
        Sensitivity.SERVER_ONLY),
    "ROBOVAST_REGISTRY_USERNAME": Known(
        _REGISTRY, "Username for an external registry an experiment image is pulled from.",
        Sensitivity.SERVER_ONLY),
    "ROBOVAST_REGISTRY_PASSWORD": Known(
        _REGISTRY, "Password for that external registry.", Sensitivity.SECRET),
    "ROBOVAST_REGISTRY_PULL_SECRET": Known(
        _REGISTRY, "Existing Kubernetes Secret used instead of the credential above.",
        Sensitivity.SERVER_ONLY),
    "ROBOVAST_REGISTRY_PUSH_SECRET": Known(
        _REGISTRY, "Kubernetes Secret the in-cluster build pushes with.",
        Sensitivity.SERVER_ONLY),
    "ROBOVAST_REGISTRY_PREFIX": Known(
        _REGISTRY, "Where in-cluster builds push; baked from this service's own Ingress.",
        Sensitivity.SERVER_ONLY),
    "ROBOVAST_BASE_EXPERIMENT_IMAGE": Known(
        _REGISTRY, "Default FROM when a project's build names no base image.",
        Sensitivity.SERVER_ONLY),
    "ROBOVAST_REGISTRY_INSECURE": Known(
        _REGISTRY, "Push over plain HTTP or an untrusted certificate."),
    "ROBOVAST_REGISTRY_CA_CONFIGMAP": Known(
        _REGISTRY, "ConfigMap holding the registry's CA, mounted into the build Job.",
        Sensitivity.SERVER_ONLY),
    "ROBOVAST_REGISTRY_CA_FILE": Known(
        _REGISTRY, "File the registry's CA was read from at setup.",
        Sensitivity.HOST_PATH),

    # -- result share ---------------------------------------------------------
    "ROBOVAST_SHARE_TYPE": Known(
        _SHARE, "Which share provider results are uploaded to: gcs, webdav, nextcloud, sftp."),
    # Nextcloud's public share link ends in the token that authenticates it — a credential
    # despite the name. See share_providers/nextcloud.py:_parse_share_url.
    "ROBOVAST_SHARE_URL": Known(
        _SHARE, "Nextcloud public share link. Its last path segment IS the credential.",
        Sensitivity.SECRET),
    "ROBOVAST_GCS_BUCKET": Known(_SHARE, "GCS bucket holding campaign archives."),
    "ROBOVAST_GCS_PREFIX": Known(_SHARE, "Key prefix within the GCS bucket."),
    "ROBOVAST_GCS_WORKERS": Known(_SHARE, "Parallel workers streaming GCS objects."),
    "ROBOVAST_GCS_ACCESS_KEY": Known(
        _SHARE, "GCS HMAC access key.", Sensitivity.SECRET),
    "ROBOVAST_GCS_SECRET_KEY": Known(
        _SHARE, "GCS HMAC secret key.", Sensitivity.SECRET),
    "ROBOVAST_GCS_KEY_FILE": Known(
        _SHARE, "Service-account JSON file the GCS credential was read from.",
        Sensitivity.HOST_PATH),
    "ROBOVAST_GCS_KEY_JSON": Known(
        _SHARE, "Inline service-account JSON for GCS.", Sensitivity.SECRET),
    "ROBOVAST_WEBDAV_URL": Known(_SHARE, "WebDAV collection archives are uploaded into."),
    "ROBOVAST_WEBDAV_USER": Known(_SHARE, "WebDAV username."),
    "ROBOVAST_WEBDAV_PASSWORD": Known(
        _SHARE, "WebDAV password.", Sensitivity.SECRET),
    "ROBOVAST_SFTP_HOST": Known(_SHARE, "SFTP server archives are uploaded to."),
    "ROBOVAST_SFTP_PORT": Known(
        _SHARE, "SFTP port.", default=str(DEFAULT_SFTP_PORT)),
    "ROBOVAST_SFTP_USER": Known(_SHARE, "SFTP username."),
    "ROBOVAST_SFTP_REMOTE_DIR": Known(_SHARE, "Remote directory archives are written to."),
    "ROBOVAST_SFTP_PASSWORD": Known(
        _SHARE, "SFTP password.", Sensitivity.SECRET),
    "ROBOVAST_SFTP_KEY_FILE": Known(
        _SHARE, "Private-key file the SFTP credential was read from.",
        Sensitivity.HOST_PATH),
    "ROBOVAST_SFTP_KEY_PEM": Known(
        _SHARE, "Inline private key for SFTP.", Sensitivity.SECRET),

    # -- notifications --------------------------------------------------------
    "ROBOVAST_NTFY_TOPIC": Known(
        _NOTIFY, "ntfy topic a campaign's start and end are pushed to. Unset means silent."),
    "ROBOVAST_NTFY_SERVER": Known(
        _NOTIFY, "ntfy instance those pushes go to.", default=DEFAULT_NTFY_SERVER),
    "ROBOVAST_NTFY_TOKEN": Known(
        _NOTIFY, "Bearer token for a protected ntfy topic.", Sensitivity.SECRET),

    # -- storage and caches ---------------------------------------------------
    "ROBOVAST_WORKSPACES_ROOT": Known(
        _STORAGE, "Root holding workspace sources.", Sensitivity.HOST_PATH),
    "ROBOVAST_BUILDS_ROOT": Known(
        _STORAGE, "Root holding image build contexts.", Sensitivity.HOST_PATH),
    "ROBOVAST_ARCHIVE_DIR": Known(
        _STORAGE, "Where the local lane writes campaign archives with no external share.",
        Sensitivity.HOST_PATH),
    "ROBOVAST_SCENE_CACHE": Known(
        _STORAGE, "Root of the shared generated-scene cache.", Sensitivity.HOST_PATH),
    "ROBOVAST_SCENE_CACHE_BYTES": Known(
        _STORAGE, "Ceiling the scene cache is trimmed to.",
        default=str(DEFAULT_MAX_CACHE_BYTES)),

    # -- process plumbing, never reported -------------------------------------
    # Set by a container entrypoint, a build, or `vast` itself. An operator did not put
    # these in a `.env` and cannot change them there, so they are not configuration.
    "ROBOVAST_COMMAND": Known("", "", Sensitivity.INTERNAL),
    "ROBOVAST_CONTAINER_COMMAND": Known("", "", Sensitivity.INTERNAL),
    "ROBOVAST_TTY": Known("", "", Sensitivity.INTERNAL),
    "ROBOVAST_STDIN_OPEN": Known("", "", Sensitivity.INTERNAL),
    "ROBOVAST_KEYS": Known("", "", Sensitivity.INTERNAL),
    "ROBOVAST_GIT_REVISION": Known("", "", Sensitivity.INTERNAL),
    "ROBOVAST_ISOLATED_COMPOSE": Known("", "", Sensitivity.INTERNAL),
    "ROBOVAST_CONFIG": Known("", "", Sensitivity.INTERNAL),
    "ROBOVAST_ENV_FILE": Known("", "", Sensitivity.INTERNAL),
    "ROBOVAST_UI_DIST": Known("", "", Sensitivity.INTERNAL),
    "ROBOVAST_DOCS_DIR": Known("", "", Sensitivity.INTERNAL),
    "ROBOVAST_EXAMPLES_DIR": Known("", "", Sensitivity.INTERNAL),
    "ROBOVAST_INSECURE_SSL": Known("", "", Sensitivity.INTERNAL),
    "ROBOVAST_CONTROLLER_IMAGE": Known("", "", Sensitivity.INTERNAL),
}


@dataclass(frozen=True)
class Described:
    """One setting as reported to one caller."""

    key: str
    group: str
    description: str
    is_set: bool
    value: Optional[str]
    default: Optional[str]
    #: Why this caller got no value though the setting is set; ``None`` when ``value``
    #: stands, and ``None`` when the setting is simply unset (``is_set`` says that).
    withheld: Optional[str]


def _git_token_is_set() -> bool:
    """Whether the private-repo token reached this service.

    Read from its mount rather than the environment: the deployment mounts it as a
    read-only file precisely so the processes the service launches do not inherit it.
    """
    if os.environ.get(GIT_TOKEN_KEY, "").strip():
        return True
    try:
        return bool(os.path.isfile(GIT_TOKEN_MOUNT)
                    and open(GIT_TOKEN_MOUNT, encoding="utf-8").read().strip())
    except OSError:
        return False


def _resolve(key: str, raw: Optional[str], loopback: bool) -> Described:
    """One environment reading, as it may be reported to this caller."""
    known = KNOWN.get(key)
    is_set = raw is not None and raw.strip() != ""
    if known is None:
        # The fail-safe. Visible, so an operator sees the setting is in force and somebody
        # is prompted to classify it; valueless, so a credential added tomorrow cannot
        # leak through a surface written today.
        return Described(key, "", "", is_set, None, None,
                         UNCLASSIFIED if is_set else None)
    if not is_set:
        return Described(key, known.group, known.description, False, None,
                         known.default, None)
    if known.sensitivity is Sensitivity.PUBLIC:
        return Described(key, known.group, known.description, True, raw,
                         known.default, None)
    if known.sensitivity is Sensitivity.HOST_PATH and loopback:
        return Described(key, known.group, known.description, True, raw,
                         known.default, None)
    return Described(key, known.group, known.description, True, None, known.default,
                     known.sensitivity.value)


def describe(loopback: bool = False) -> list[Described]:
    """Every setting in force in this service, plus the known ones that are not.

    Args:
        loopback: whether the caller reached this service over the loopback interface, and
            may therefore be shown host paths — the rule ``/version`` already applies to
            ``results_root`` / ``sources_root``.

    The environment decides what is reported; :data:`KNOWN` decides only how. A key it does
    not cover still appears, without its value.
    """
    env = {k: v for k, v in os.environ.items() if k.startswith(PREFIX)}
    internal = {k for k, v in KNOWN.items() if v.sensitivity is Sensitivity.INTERNAL}
    # Known-but-unset too, so "ntfy is not configured" is a row an operator can read rather
    # than an absence they have to notice.
    keys = (set(env) | set(KNOWN)) - internal
    rows = [_resolve(k, env.get(k), loopback) for k in sorted(keys)]

    # The one setting os.environ cannot answer for.
    known_git = KNOWN[GIT_TOKEN_KEY]
    rows = [r for r in rows if r.key != GIT_TOKEN_KEY]
    rows.append(Described(
        GIT_TOKEN_KEY, known_git.group, known_git.description, _git_token_is_set(),
        None, None, Sensitivity.SECRET.value if _git_token_is_set() else None))
    return sorted(rows, key=lambda r: (r.group, r.key))


def how_to_change() -> str:
    """How a settings change reaches THIS deployment, in the operator's terms.

    Two genuinely different answers, and this process is the only party that knows which
    applies: a pod loads its Secrets through ``envFrom`` at container start and never
    again, so nothing short of a roll re-reads them -- which is why ``vast service upgrade
    --no-restart`` says out loud that it did not. A ``vast serve`` simply inherits the
    environment of whoever started it.
    """
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return ("Edit your .env, then run 'vast service upgrade' (without --no-restart): "
                "the pod reads its Secrets once at container start, so only a restart "
                "picks up a change.")
    return "Edit your .env, then restart 'vast serve' so it inherits the new environment."
