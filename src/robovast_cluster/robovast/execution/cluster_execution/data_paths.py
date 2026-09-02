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

"""Where each of this deployment's on-disk tenants lives.

One module answers that, because three callers must agree on it: the ``setup`` CLI, which
takes the flags; :mod:`.cluster_setup`, which routes them to the pods; and the recovery
readers, which fill in what a later run did not restate. A precedence rule carried three
times is a base path that reaches the registry and not the workspaces -- a split placement
nobody asked for, on a deployment whose whole point is that its data is in one place.

**The tenant table is the single home for the default paths.** The modules that build each
volume import them from here rather than declaring their own, so a default cannot be changed
in one place and read from another.

Deliberately free of Kubernetes and of the modules that talk to it: ``cli`` is a plugin
imported on every ``vast`` invocation, and a resolver that dragged the cluster stack in would
make ``vast login`` pay for it.
"""

from pathlib import PurePosixPath
from typing import NamedTuple

#: Default host paths, one per tenant. These are what a deployment given no placement flags
#: has always used, and they must stay that way: a cluster re-run with a changed default
#: would point at a fresh empty directory beside its data and report success.
DEFAULT_WORKSPACES_HOST_PATH = "/var/lib/robovast-workspaces"
DEFAULT_RESULTS_HOST_PATH = "/var/lib/robovast-results"
DEFAULT_REGISTRY_HOST_PATH = "/var/lib/robovast-registry"
DEFAULT_BUILDKITD_HOST_PATH = "/data/robovast-buildkit"


class Tenant(NamedTuple):
    """One thing this deployment keeps on disk, and how it is placed.

    *derived_from* names the tenant this one sits beside. A derived tenant takes no flags of
    its own: it follows its parent's directory and its parent's backing, which is what makes
    "these two are always together" structural instead of a rule someone has to enforce.
    """

    name: str
    default_path: str
    derived_from: str = ""

    @property
    def is_derived(self) -> bool:
        return bool(self.derived_from)


#: Every tenant, in the order an operator meets them. ``results`` follows ``workspaces``
#: because the service pod holds both and mirrors a campaign between them; the store and its
#: index join this table when the store stops being an ``emptyDir``, one row each, needing no
#: change to the rules below.
TENANTS = (
    Tenant("workspaces", DEFAULT_WORKSPACES_HOST_PATH),
    Tenant("results", DEFAULT_RESULTS_HOST_PATH, derived_from="workspaces"),
    Tenant("registry", DEFAULT_REGISTRY_HOST_PATH),
    Tenant("buildkit", DEFAULT_BUILDKITD_HOST_PATH),
)

TENANTS_BY_NAME = {t.name: t for t in TENANTS}

#: The tenants an operator can place directly. The derived ones are absent on purpose -- a
#: flag for one could only ever agree with its parent or be refused.
PLACEABLE = tuple(t.name for t in TENANTS if not t.is_derived)


class Placement(NamedTuple):
    """Where one tenant's bytes go: a node directory, or a volume a class provisions.

    Exactly one is meaningful. ``storage_class`` wins where both are set, which is why
    setting both is refused rather than resolved -- see :func:`refuse_conflicts`.
    """

    path: str
    storage_class: str = ""

    @property
    def is_node_local(self) -> bool:
        return not self.storage_class


def derive_sibling(path: str, of: str, to: str) -> str:
    """The path beside *path* that holds *to* where *path* holds *of*.

    ``/var/lib/robovast-workspaces`` -> ``/var/lib/robovast-results``, and
    ``/media/data/workspaces`` -> ``/media/data/results``: the token is replaced inside the
    final component, so a deployment that renamed its directories keeps its own convention
    rather than having ours imposed halfway down the path.

    A final component naming no tenant (``/mnt/minio``) gets a suffix instead of a silent
    guess, so the two still land beside each other and the result still says which is which.
    """
    tail = PurePosixPath(path)
    name = tail.name.replace(of, to) if of in tail.name else f"{tail.name}-{to}"
    return str(tail.parent / name)


def resolve(explicit=None, *, data_root: str = "") -> dict:
    """Every tenant's :class:`Placement` as *this invocation* states it.

    *explicit* is a flat ``{"<tenant>_path": ..., "<tenant>_class": ...}`` dict -- the shape
    the CLI collects -- so no caller has to reshape anything to ask this question.

    **An unplaced tenant comes back empty, not defaulted**, and that is the whole subtlety
    here. Downstream, an empty path means "nobody said", which is what lets a plain re-run
    recover the placement a live deployment already has; filling in the default would hand
    that deployment its default explicitly and move it off its own data while reporting
    success. The defaults live in :data:`TENANTS` and are applied by the volume builders,
    at the one point that knows nothing else answered.

    So the precedence an operator sees is:

    #. **What they stated**, by flag or environment. Click cannot tell those apart and must
       not: both are someone saying where this goes.
    #. **``--data-root``**, placing every unstated tenant at ``<root>/<tenant>``.
    #. **What the live cluster is already doing** -- applied downstream, because only the
       deploy path can read it. It exists so an ``upgrade`` passing no storage arguments
       cannot revert a placement: "not passed" must not read as "unpinned".
    #. **The tenant's default.**

    ``--data-root`` outranks recovery deliberately, and that ordering is the whole difference
    between the two. Recovery answers for a caller that said nothing; a root is a caller
    saying where this deployment's data goes. Ranked the other way it would move the registry
    and the build cache and leave the workspaces behind -- the split placement this module
    exists to prevent.
    """
    explicit = explicit or {}
    placements = {}

    def stated(tenant, field):
        return (explicit.get(f"{tenant}_{field}") or "").strip()

    for tenant in TENANTS:
        if tenant.is_derived:
            continue
        path = stated(tenant.name, "path")
        if not path and data_root:
            path = f"{data_root.rstrip('/')}/{tenant.name}"
        placements[tenant.name] = Placement(path, stated(tenant.name, "class"))

    # After the parents, because a derived tenant is a function of one -- including when the
    # parent is unplaced, where the derived one is unplaced too and both take their defaults.
    for tenant in TENANTS:
        if not tenant.is_derived:
            continue
        parent = placements[tenant.derived_from]
        placements[tenant.name] = Placement(
            derive_sibling(parent.path, tenant.derived_from, tenant.name)
            if parent.path else "",
            parent.storage_class)
    return placements


def refuse_conflicts(explicit=None, *, data_root: str = "", sizes=None) -> None:
    """Raise on a placement setting that cannot be honoured, naming both halves of it.

    Two settings for one tenant is the one worth catching: a class provisions a volume and a
    path names a node directory, so a tenant given both gets the class and silently ignores
    the path -- reported as configured, placed somewhere else. A size without a class is the
    same failure one step smaller: it sizes a claim that will not be created.

    *sizes* maps a tenant to the size requested for its claim; only tenants that have a size
    flag appear in it.
    """
    explicit = explicit or {}
    sizes = sizes or {}
    for name in PLACEABLE:
        path = (explicit.get(f"{name}_path") or "").strip()
        storage_class = (explicit.get(f"{name}_class") or "").strip()
        if path and storage_class:
            raise ValueError(
                f"--{name}-path and --{name}-class both place the {name}, and they cannot "
                f"both apply: a class provisions a volume, a path names a directory on the "
                f"node. Pass one.")
    for name, size in sizes.items():
        if (size or "").strip() and not (explicit.get(f"{name}_class") or "").strip():
            raise ValueError(
                f"--{name}-size asks for a {size} volume, but without --{name}-class the "
                f"{name} is a directory on the node and nothing provisions a volume to size. "
                f"Its bound is the node's disk.")
    if data_root and not data_root.startswith("/"):
        raise ValueError(
            f"--data-root must be an absolute path on the node; got {data_root!r}. It is "
            f"resolved by the kubelet on whichever node holds this deployment's data, not "
            f"relative to where 'vast' runs.")
