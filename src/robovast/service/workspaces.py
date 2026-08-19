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

"""Server-side **workspaces** — the editable project inputs a campaign runs from.

A workspace holds only *inputs* (``.vast`` / ``.osc`` / run files / binaries), so
clients need no filesystem of their own and the service may live on another host.

**Workspaces are independent of campaigns.** Running a campaign produces a
self-contained campaign directory (it snapshots its project into ``_config/``),
addressed by ``campaign_id`` alone. Editing or deleting a workspace afterwards
never affects an existing campaign, and there is no campaign→workspace link.

Token economics drive the file API (see the plan):

* **Inline writes are restricted to ``.vast``/``.osc``** — the small text an LLM
  authors and iterates on. Their content passes through the model, so it costs
  tokens twice (generation + every later turn in history); ``edit_file``
  exists so the validate→fix loop sends a small diff instead of a whole file.
* **Everything else must use the HTTP PUT side channel** (:meth:`WorkspaceStore.
  create_upload` → a TTL-scoped token; the client ``curl``s the bytes straight in),
  so run files, notebooks and binaries never enter the token stream.

Executability is preserved end-to-end by reusing the *existing* mechanism: the
bit is set here (explicit flag or shebang auto-detect) and
``in_pod_storage._is_executable``/``_EXECUTABLE_META`` already carry it through
the object store (S3 ``x-amz-meta-executable`` / GCS blob metadata).

The registry is one JSON file guarded by an ``fcntl`` lock and replaced by an
atomic temp-file rename.
"""

import errno
import fcntl
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

#: File types an LLM may write inline (everything else → create_upload).
INLINE_EXTENSIONS = (".vast", ".osc")

REGISTRY_FILENAME = "registry.json"
LOCK_FILENAME = "registry.lock"
PROJECT_DIRNAME = "project"

#: Default lifetime of an upload token; the PUT must arrive within this window.
UPLOAD_TTL_SECONDS = 600

# The client half of the workspace vocabulary. A client pushing a directory must agree
# with this module about which files belong to a project, but must not have to install
# the registry and the store to find out -- so those two live in robovast.client and this
# module is one of their callers, not their home.
from robovast.client.workspaces import PINNED_SKIP_DIRS, is_skipped  # pylint: disable=wrong-import-position


def default_workspaces_root() -> Path:
    """Root for workspace storage (``ROBOVAST_WORKSPACES_ROOT`` or ``~/.robovast``).

    Server-side: it is where *this* process keeps workspaces. A client install has no
    such store, which is why this stayed here when ``is_skipped`` moved to the client.
    """
    env = os.environ.get("ROBOVAST_WORKSPACES_ROOT")
    if env:
        return Path(env)
    return Path.home() / ".robovast" / "workspaces"


class WorkspaceError(ValueError):
    """Invalid workspace request (bad id, bad path, wrong file type)."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class WorkspaceRegistry:
    """Crash-safe JSON registry of workspaces (flock + atomic rename).

    Beyond the persisted, editable workspaces it also carries **one** optional
    **static** (pinned, read-only) workspace given at construction — a directory used
    *in place*, never copied into the store. It is in-memory only (never written to
    ``registry.json``): a ``vast serve --workspace-dir DIR`` derives it afresh each
    start with a path-stable id, so links survive a restart without a
    ``workspace init`` upload. See :meth:`add_static` / :meth:`is_pinned`.

    Exactly one, deliberately: a pinned directory holds as many ``.vast`` files as
    the caller likes (selected per campaign by ``config_path``), so N directories add
    no expressiveness — while N arbitrary host paths would leave the service with no
    single sources root to publish in ``get_service_info``.
    """

    def __init__(self, root=None, static_dir=None):
        self.root = Path(root) if root else default_workspaces_root()
        self.registry_path = self.root / REGISTRY_FILENAME
        self.lock_path = self.root / LOCK_FILENAME
        #: workspace_id -> registry entry, for the pinned read-only dir (in-memory).
        self._static: dict[str, dict] = {}
        #: workspace_id -> on-disk source Path used directly (read-only).
        self._static_paths: dict[str, Path] = {}
        if static_dir is not None:
            # Accept a bare path or a (path, name) pair.
            if isinstance(static_dir, (tuple, list)):
                self.add_static(static_dir[0],
                                static_dir[1] if len(static_dir) > 1 else "")
            else:
                self.add_static(static_dir)

    def add_static(self, path, name: str = "") -> dict:
        """Pin *path* as a workspace used **in place**; return its entry.

        The id is derived from the resolved path (stable across restarts, so the UI link
        keeps working). The directory is **writable**: an edit in the Config tab lands on
        the real file, which is what makes a local project editable from the browser at all.
        Without it the web UI could not replace the desktop editor's Open/Save for anyone
        working on a git-tracked project -- the only route was to copy the project into the
        store, edit the copy, and copy it back.

        What stays refused is *deleting the workspace*: the directory is the caller's, not
        the store's, so unpinning it is a ``--workspace-dir`` flag rather than a DELETE.
        """
        import hashlib
        p = Path(path).expanduser().resolve()
        if not p.is_dir():
            raise WorkspaceError(f"workspace dir does not exist: {path}")
        workspace_id = "ws-" + hashlib.sha1(str(p).encode()).hexdigest()[:12]
        entry = {
            "workspace_id": workspace_id,
            "name": name or p.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "read_only": False,
            "source_dir": str(p),
        }
        self._static[workspace_id] = entry
        self._static_paths[workspace_id] = p
        logger.info("Pinned workspace %s -> %s (edits land on these files)", workspace_id, p)
        return entry

    def require_syncable(self, workspace_id: str) -> None:
        """Refuse a **bulk directory sync** into a pinned workspace.

        Individual edits are allowed -- that is the point of a pinned directory. A whole-tree
        sync is a different act: it overwrites every file at once and, with ``--prune``,
        deletes the ones the source does not have. Against a directory the caller owns (a git
        working tree, typically) that is a destructive operation with a plain alternative,
        which is to edit the directory itself. Nothing is gained by mirroring a directory onto
        itself, and a mirror of a *different* directory onto it is almost certainly a mistake.
        """
        if self.is_pinned(workspace_id):
            raise WorkspaceError(
                f"workspace {workspace_id!r} is a directory pinned in place "
                "(vast serve --workspace-dir), so a whole-directory sync would overwrite -- "
                "and with --prune delete -- files in that directory. Individual edits through "
                "the service are fine; to replace the tree, edit it on disk.")

    def is_pinned(self, workspace_id: str) -> bool:
        """True if *workspace_id* is a directory used in place (``--workspace-dir``).

        Pinned is about *where the files live*, not about whether they may be written: the
        listing filter below skips what such a tree carries and ``.git`` is not ours, and
        deleting the workspace is refused. Everything else is an ordinary workspace.
        """
        return workspace_id in self._static

    def ensure_dirs(self):
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self):
        self.ensure_dirs()
        with open(self.lock_path, "w", encoding="utf-8") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def _read_unlocked(self) -> dict:
        try:
            with open(self.registry_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("workspace registry unreadable (%s); starting empty", e)
            return {}

    def _write_unlocked(self, data: dict) -> None:
        """Atomically replace the registry file (temp file + rename)."""
        self.ensure_dirs()
        fd, tmp = tempfile.mkstemp(dir=str(self.root), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp, self.registry_path)
        except BaseException:
            with _suppress_oserror():
                os.unlink(tmp)
            raise

    def project_dir(self, workspace_id: str) -> Path:
        # A pinned dir is used in place; everything else lives under project/.
        if workspace_id in self._static_paths:
            return self._static_paths[workspace_id]
        return self.root / workspace_id / PROJECT_DIRNAME

    def create(self, name: str = "") -> dict:
        """Create a workspace (id + ``project/`` dir) and register it.

        A requested *name* that already exists gets an incrementing ``-N`` suffix
        (``foo`` → ``foo-2`` → ``foo-3``) so repeated ``workspace init`` of the same
        directory stays distinguishable in the UI dropdown instead of piling up
        identical labels. The suffixing happens under the lock (against both
        persisted and pinned names) so concurrent creates can't collide; the caller
        gets back the *final* name in the returned entry.
        """
        workspace_id = f"ws-{secrets.token_hex(6)}"
        with self._locked():
            data = self._read_unlocked()
            entry = {
                "workspace_id": workspace_id,
                "name": self._unique_name(name or workspace_id, data),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            data[workspace_id] = entry
            self._write_unlocked(data)
        self.project_dir(workspace_id).mkdir(parents=True, exist_ok=True)
        logger.info("Created workspace %s (%s)", workspace_id, entry["name"])
        return entry

    def _unique_name(self, name: str, data: dict) -> str:
        """Return *name*, or ``name-2``/``-3``/… if it collides with an existing one.

        Considers both persisted (*data*, already read under the lock) and pinned
        read-only names, so an init'd copy never shadows-by-name a pinned dir."""
        taken = {e.get("name") for e in data.values()}
        taken |= {e.get("name") for e in self._static.values()}
        if name not in taken:
            return name
        n = 2
        while f"{name}-{n}" in taken:
            n += 1
        return f"{name}-{n}"

    def list(self) -> list[dict]:
        with self._locked():
            data = self._read_unlocked()
        merged = {**data, **self._static}  # pinned dirs shown alongside persisted ones
        return sorted(merged.values(), key=lambda e: e.get("created_at", ""), reverse=True)

    def get(self, workspace_id: str) -> dict | None:
        if workspace_id in self._static:
            return self._static[workspace_id]
        with self._locked():
            return self._read_unlocked().get(workspace_id)

    def resolve(self, id_or_name: str) -> str:
        """Map an id-*or-name* to a concrete ``workspace_id``.

        An exact id match always wins; otherwise fall back to a unique match on
        ``name``. Raises :class:`WorkspaceError` when nothing matches or when a
        name is ambiguous (then the caller must use the ``ws-…`` id).
        """
        with self._locked():
            data = self._read_unlocked()
        merged = {**data, **self._static}
        if id_or_name in merged:
            return id_or_name
        matches = [wid for wid, e in merged.items() if e.get("name") == id_or_name]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            known = ", ".join(
                f"{wid} ({e.get('name')})" if e.get("name") else wid
                for wid, e in sorted(merged.items())) or "(none yet)"
            raise WorkspaceError(
                f"unknown workspace {id_or_name!r}. Known: {known}. Create one with "
                "create_workspace, or pin a directory with "
                "'vast serve --workspace-dir <dir>'.")
        raise WorkspaceError(
            f"workspace name {id_or_name!r} is ambiguous ({len(matches)} matches); "
            "delete it by its ws-… id instead")

    def require(self, id_or_name: str) -> dict:
        return self.get(self.resolve(id_or_name))

    def delete(self, id_or_name: str) -> None:
        """Remove a workspace and its inputs (accepts an id or a name).

        Safe by construction: campaigns are self-contained and independent, so
        this never affects results produced from this workspace.
        """
        import shutil
        workspace_id = self.require(id_or_name)["workspace_id"]
        if workspace_id in self._static:
            raise WorkspaceError(
                f"workspace {workspace_id!r} is a directory pinned in place "
                "(vast serve --workspace-dir). Its files may be edited through the service, "
                "but the directory is yours rather than the store's, so it is unpinned by "
                "dropping the --workspace-dir flag, not by deleting it here.")
        with self._locked():
            data = self._read_unlocked()
            data.pop(workspace_id, None)
            self._write_unlocked(data)
        shutil.rmtree(self.root / workspace_id, ignore_errors=True)
        logger.info("Deleted workspace %s", workspace_id)


# ---------------------------------------------------------------------------
# Upload tokens (TTL-scoped, one-time)
# ---------------------------------------------------------------------------


class _UploadTokens:
    """One-time, TTL-expiring upload grants: token → (workspace, path, exec, expiry)."""

    def __init__(self, ttl_seconds=UPLOAD_TTL_SECONDS, now_fn=time.time):
        self.ttl = ttl_seconds
        self._now = now_fn
        self._tokens: dict[str, dict] = {}
        self._lock = threading.Lock()

    def issue(self, workspace_id: str, path: str, executable: bool = False) -> dict:
        token = secrets.token_urlsafe(24)
        grant = {"workspace_id": workspace_id, "path": path,
                 "executable": bool(executable), "expires_at": self._now() + self.ttl}
        with self._lock:
            self._purge()
            self._tokens[token] = grant
        return {"token": token, "expires_in": self.ttl, **grant}

    def redeem(self, token: str) -> dict:
        """Consume a token (one-time). Raises if unknown or expired."""
        with self._lock:
            self._purge()
            grant = self._tokens.pop(token, None)
        if grant is None:
            raise WorkspaceError(
                "upload token is unknown or expired; call create_upload again")
        return grant

    def _purge(self):
        now = self._now()
        for tok in [t for t, g in self._tokens.items() if g["expires_at"] <= now]:
            self._tokens.pop(tok, None)


# ---------------------------------------------------------------------------
# Store (file operations, confined to a workspace's project/ dir)
# ---------------------------------------------------------------------------


class WorkspaceStore:
    """File operations on workspaces, with strict path confinement."""

    def __init__(self, registry: WorkspaceRegistry | None = None, tokens=None,
                 workspace_dir=None):
        self.registry = registry or WorkspaceRegistry(static_dir=workspace_dir)
        self.tokens = tokens or _UploadTokens()

    # -- read-only guard ----------------------------------------------------

    # -- path safety --------------------------------------------------------

    def _safe_join(self, workspace_id: str, rel_path: str) -> Path:
        """Resolve *rel_path* inside this workspace, refusing any escape.

        The confinement itself is :func:`~robovast.client.safe_path.safe_join`, shared
        with the campaign results tree; this only supplies the workspace root and
        re-labels the failure as a :class:`WorkspaceError` so callers keep mapping it
        the way they always have.
        """
        from robovast.client.safe_path import UnsafePathError, safe_join
        try:
            return safe_join(self.registry.project_dir(workspace_id), rel_path)
        except UnsafePathError as e:
            raise WorkspaceError(str(e)) from e

    @staticmethod
    def _require_inline_type(rel_path: str) -> None:
        if not rel_path.lower().endswith(INLINE_EXTENSIONS):
            raise WorkspaceError(
                f"inline writes are limited to {'/'.join(INLINE_EXTENSIONS)} files; "
                f"use create_upload() for {rel_path!r} (keeps bytes out of the token stream)")

    # -- inline authoring (.vast/.osc only) ---------------------------------

    def write_file(self, workspace_id: str, rel_path: str, content: str) -> dict:
        workspace_id = self.registry.require(workspace_id)["workspace_id"]
        self._require_inline_type(rel_path)
        target = self._safe_join(workspace_id, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return self._file_meta(target, rel_path)

    def edit_file(self, workspace_id: str, rel_path: str, old_string: str,
                  new_string: str) -> dict:
        """Replace *old_string* once — the token-cheap validate→fix loop."""
        workspace_id = self.registry.require(workspace_id)["workspace_id"]
        self._require_inline_type(rel_path)
        target = self._safe_join(workspace_id, rel_path)
        if not target.is_file():
            raise WorkspaceError(
                f"no such file in workspace: {rel_path!r} — list the workspace "
                f"with list_files('/sources/{workspace_id}/') to see what is there.")
        text = target.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count == 0:
            raise WorkspaceError(f"old_string not found in {rel_path!r}")
        if count > 1:
            raise WorkspaceError(
                f"old_string is not unique in {rel_path!r} ({count} matches); "
                "include more context")
        target.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
        return self._file_meta(target, rel_path)

    def resolve(self, workspace_id: str, rel_path: str = "") -> Path:
        """Absolute path of *rel_path* in the workspace — the root when it is empty.

        The one seam the generic ``/sources`` file operations resolve through, so they
        inherit this store's confinement instead of re-deriving the root.
        """
        workspace_id = self.registry.require(workspace_id)["workspace_id"]
        if not rel_path:
            return self.registry.project_dir(workspace_id)
        return self._safe_join(workspace_id, rel_path)

    def skip_entry(self, workspace_id: str):
        """Predicate for entries a listing must hide, or ``None`` when none do.

        A pinned dir is a live project tree, so skip what ``workspace init`` also skips
        — hidden files (``.git``/``.cache``) and campaign outputs (``results/``). A
        normal ``project/`` contains neither, so this is a no-op for those.

        Shares :func:`is_skipped` with the push side. They must agree: a
        ``sync_directory_to_workspace(prune=True)`` deletes what a listing shows but a
        push would never re-upload, so a rule added to one and not the other makes
        prune destructive.
        """
        workspace_id = self.registry.require(workspace_id)["workspace_id"]
        if not self.registry.is_pinned(workspace_id):
            return None
        return lambda rel, _is_dir: is_skipped(rel, PINNED_SKIP_DIRS)

    def delete_file(self, workspace_id: str, rel_path: str) -> None:
        workspace_id = self.registry.require(workspace_id)["workspace_id"]
        target = self._safe_join(workspace_id, rel_path)
        if not target.is_file():
            raise WorkspaceError(
                f"no such file in workspace: {rel_path!r} — list the workspace "
                f"with list_files('/sources/{workspace_id}/') to see what is there.")
        target.unlink()

    # -- upload side channel (everything that is not .vast/.osc) ------------

    def create_upload(self, workspace_id: str, rel_path: str,
                      executable: bool = False) -> dict:
        """Issue a one-time, TTL-scoped grant for an HTTP PUT of *rel_path*."""
        workspace_id = self.registry.require(workspace_id)["workspace_id"]
        self._safe_join(workspace_id, rel_path)  # validate up front
        return self.tokens.issue(workspace_id, rel_path, executable=executable)

    def write_upload(self, token: str, data: bytes) -> dict:
        """Redeem *token* and write *data* into the workspace (any file type).

        Returns the file metadata **plus** the ``workspace_id`` the token named: the
        caller has only an opaque token, so without it there is no way to say which
        address was written.
        """
        grant = self.tokens.redeem(token)
        target = self._safe_join(grant["workspace_id"], grant["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if grant["executable"] or _has_shebang(data):
            # From here the existing _is_executable/_EXECUTABLE_META machinery
            # carries the bit through the object store and into the campaign.
            target.chmod(target.stat().st_mode | 0o111)
        return {"workspace_id": grant["workspace_id"],
                **self._file_meta(target, grant["path"])}

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _file_meta(path: Path, rel_path: str) -> dict:
        import hashlib
        data = path.read_bytes()
        return {
            "path": rel_path,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "executable": bool(path.stat().st_mode & 0o111),
        }


def _has_shebang(data: bytes) -> bool:
    """True if *data* starts with ``#!`` — the Unix 'this is a program' marker."""
    return data[:2] == b"#!"


@contextmanager
def _suppress_oserror():
    try:
        yield
    except OSError as e:
        if e.errno != errno.ENOENT:
            logger.debug("ignored OSError: %s", e)
