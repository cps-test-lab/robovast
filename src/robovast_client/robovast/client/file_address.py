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

"""One address space for every file the service can reach.

An address is ``/<namespace>/<owner>/<path>`` and is **also the service's URL**, so the
string a caller passes is the string it can ``GET``:

``/results/<campaign_id>/<path>``
    A campaign's outputs. **Read-only** — on the cluster the local tree is a cache of
    immutable objects, so a write there would silently vanish.
``/sources/<workspace_id>/<path>``
    A workspace's authored inputs. Writable.

The namespace *is* the permission, dispatched once instead of enforced per operation,
and each confines against its own root — never the other's (see
:mod:`robovast.client.safe_path`).

These are separate top-level namespaces from ``/campaigns/{id}/`` and
``/workspaces/{id}/`` on purpose. Those are **control** namespaces whose literal
segments (``status``, ``logs``, ``query``, ``validate`` …, plus every name a service
endpoint plugin registers) would shadow a user-chosen config or file name. Keeping
content in its own namespace makes "no reserved words, ever" true by construction rather
than by whichever control routes happen to exist today.

The path after the namespace is the **real on-disk relative path**: a run artifact is
``<config_name>/<run_id>/<path>``, not a synthetic ``run-files/`` segment matching no
directory.
"""

from robovast.client.safe_path import UnsafePathError, check_relative

#: The read-only namespace: campaign outputs, addressed by campaign id.
RESULTS = "results"
#: The writable namespace: workspace inputs, addressed by workspace id.
SOURCES = "sources"

#: Every namespace, mapped to whether an address in it may be written.
NAMESPACES = {RESULTS: False, SOURCES: True}

#: What ``<owner>`` is called in each namespace — used in error messages so a caller is
#: told which id to supply, not merely that one is missing.
_OWNER_NAME = {RESULTS: "campaign_id", SOURCES: "workspace_id"}

_EXAMPLE = "/results/<campaign_id>/_execution/outcome.json"


class AddressError(ValueError):
    """A malformed file address. A ``ValueError``, so it maps to 400 like the rest."""


def _hint(address: str, problem: str) -> "AddressError":
    """Build an error that teaches the address space instead of only refusing.

    A caller that got the shape wrong — a bare relative path, a stale ``run-files/``
    segment, an unknown namespace — cannot fix it from "invalid path". Every failure
    therefore states the expected form and one concrete example.
    """
    return AddressError(
        f"{problem}: {address!r}. Expected '/<namespace>/<owner>/<path>' where "
        f"namespace is one of {sorted(NAMESPACES)} — e.g. {_EXAMPLE!r}. The path after "
        "the owner is the real on-disk relative path (a run artifact is "
        "'<config_name>/<run_id>/<file>').")


def parse_address(address: str) -> tuple[str, str, str]:
    """Split an address into ``(namespace, owner, rel_path)``.

    ``rel_path`` may be empty — that addresses the owner's root, which is a directory
    and therefore only listable. A trailing slash is **not** significant here; it is a
    hint about the caller's intent, not part of the path. Use :func:`is_directory` to
    read it, and note that both transports treat it as a hint rather than a demand:
    listing a path written without one works, so the same call means the same thing
    whether it went over HTTP or in-process.

    Args:
        address: ``/<namespace>/<owner>[/<path>]``. A leading slash is optional so a
            caller that stripped it still succeeds.

    Returns:
        ``(namespace, owner, rel_path)`` with ``rel_path`` in POSIX form.

    Raises:
        AddressError: On an unknown namespace, a missing owner, or a path that escapes
            its root (``..``, absolute, ``~``).
    """
    if not isinstance(address, str) or not address.strip():
        raise _hint(str(address), "address must not be empty")

    parts = address.strip().strip("/").split("/")
    namespace = parts[0]
    if namespace not in NAMESPACES:
        raise _hint(address, f"unknown namespace {namespace!r}")
    if len(parts) < 2 or not parts[1]:
        raise _hint(address, f"missing {_OWNER_NAME[namespace]}")

    owner = parts[1]
    rel_path = "/".join(parts[2:])
    if rel_path:
        try:
            check_relative(rel_path)
        except UnsafePathError as e:
            raise _hint(address, str(e)) from e
    return namespace, owner, rel_path


def is_directory(address: str) -> bool:
    """Whether *address* is written as a directory (a trailing ``/``).

    A **hint**, never a requirement: a transport that finds a directory at an address
    written without one still lists it. The alternative — making the character
    load-bearing — gives one interface call two answers depending on which client made
    it, which is exactly what an address space is for avoiding.
    """
    return isinstance(address, str) and address.rstrip().endswith("/")


def as_directory(address: str) -> str:
    """*address* in directory form (a trailing ``/``).

    The HTTP binding reads the trailing slash to tell "list this" from "read this",
    so a client that means to list normalizes here rather than making every caller
    remember the character. That is what keeps ``list_files("/results/c/_execution")``
    mean the same thing in-process and over HTTP.
    """
    return address if address.endswith("/") else f"{address}/"


def format_address(namespace: str, owner: str, rel_path: str = "") -> str:
    """Build the address (and thus the URL) for *rel_path* under *owner*.

    A trailing slash in *rel_path* is preserved, so a caller that holds a directory
    path keeps the mark a listing puts on its directory entries and can concatenate
    the two. An owner root is always a directory.
    """
    if namespace not in NAMESPACES:
        raise _hint(f"/{namespace}/{owner}", f"unknown namespace {namespace!r}")
    base = f"/{namespace}/{owner}"
    return f"{base}/{rel_path.lstrip('/')}" if rel_path else f"{base}/"


def is_writable(namespace: str) -> bool:
    """Whether the namespace accepts writes. ``/results`` never does."""
    return NAMESPACES.get(namespace, False)


def require_writable(address: str, namespace: str) -> None:
    """Refuse a mutation of a read-only namespace, saying where writes *do* go."""
    if not is_writable(namespace):
        raise AddressError(
            f"{address!r} is read-only: campaign results are immutable "
            f"(on the cluster they are object-store objects and a local write would be "
            f"a cache edit that vanishes). Author inputs under '/{SOURCES}/<workspace_id>/'.")
