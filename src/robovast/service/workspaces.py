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
  tokens twice (generation + every later turn in history); ``edit_project_file``
  exists so the validate→fix loop sends a small diff instead of a whole file.
* **Everything else must use the HTTP PUT side channel** (:meth:`WorkspaceStore.
  create_upload` → a TTL-scoped token; the client ``curl``s the bytes straight in),
  so run files, notebooks and binaries never enter the token stream.

Executability is preserved end-to-end by reusing the *existing* mechanism: the
bit is set here (explicit flag or shebang auto-detect) and
``in_pod_storage._is_executable``/``_EXECUTABLE_META`` already carry it through
the object store (S3 ``x-amz-meta-executable`` / GCS blob metadata).

The registry mirrors :mod:`robovast.mcp_server.campaign_registry`: one JSON file
guarded by an ``fcntl`` lock and replaced by an atomic temp-file rename.
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


def default_workspaces_root() -> Path:
    """Root for workspace storage (``ROBOVAST_WORKSPACES_ROOT`` or ``~/.robovast``)."""
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
    """Crash-safe JSON registry of workspaces (flock + atomic rename)."""

    def __init__(self, root=None):
        self.root = Path(root) if root else default_workspaces_root()
        self.registry_path = self.root / REGISTRY_FILENAME
        self.lock_path = self.root / LOCK_FILENAME

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
        return self.root / workspace_id / PROJECT_DIRNAME

    def create(self, name: str = "") -> dict:
        """Create a workspace (id + ``project/`` dir) and register it."""
        workspace_id = f"ws-{secrets.token_hex(6)}"
        entry = {
            "workspace_id": workspace_id,
            "name": name or workspace_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._locked():
            data = self._read_unlocked()
            data[workspace_id] = entry
            self._write_unlocked(data)
        self.project_dir(workspace_id).mkdir(parents=True, exist_ok=True)
        logger.info("Created workspace %s (%s)", workspace_id, entry["name"])
        return entry

    def list(self) -> list[dict]:
        with self._locked():
            data = self._read_unlocked()
        return sorted(data.values(), key=lambda e: e.get("created_at", ""), reverse=True)

    def get(self, workspace_id: str) -> dict | None:
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
        if id_or_name in data:
            return id_or_name
        matches = [wid for wid, e in data.items() if e.get("name") == id_or_name]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise WorkspaceError(f"unknown workspace {id_or_name!r}")
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

    def __init__(self, registry: WorkspaceRegistry | None = None, tokens=None):
        self.registry = registry or WorkspaceRegistry()
        self.tokens = tokens or _UploadTokens()

    # -- path safety --------------------------------------------------------

    def _safe_join(self, workspace_id: str, rel_path: str) -> Path:
        """Resolve *rel_path* inside the workspace, refusing any escape.

        Rejects absolute paths and ``..`` segments, and verifies the *resolved*
        path stays under ``project/`` so a symlink cannot point outside.
        """
        if not rel_path or rel_path.strip() == "":
            raise WorkspaceError("path must not be empty")
        if os.path.isabs(rel_path) or rel_path.startswith("~"):
            raise WorkspaceError(f"path must be relative to the workspace: {rel_path!r}")
        parts = Path(rel_path).parts
        if any(p == ".." for p in parts):
            raise WorkspaceError(f"path must not contain '..': {rel_path!r}")

        base = self.registry.project_dir(workspace_id).resolve()
        target = (base / rel_path)
        # resolve(strict=False) collapses symlinks for existing prefixes
        resolved = target.resolve()
        if resolved != base and base not in resolved.parents:
            raise WorkspaceError(f"path escapes the workspace: {rel_path!r}")
        return resolved

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
            raise WorkspaceError(f"no such file in workspace: {rel_path!r}")
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

    def read_file(self, workspace_id: str, rel_path: str) -> str:
        workspace_id = self.registry.require(workspace_id)["workspace_id"]
        target = self._safe_join(workspace_id, rel_path)
        if not target.is_file():
            raise WorkspaceError(f"no such file in workspace: {rel_path!r}")
        return target.read_text(encoding="utf-8", errors="replace")

    def list_files(self, workspace_id: str) -> list[dict]:
        workspace_id = self.registry.require(workspace_id)["workspace_id"]
        base = self.registry.project_dir(workspace_id)
        if not base.is_dir():
            return []
        out = []
        for p in sorted(base.rglob("*")):
            if p.is_file():
                out.append(self._file_meta(p, str(p.relative_to(base))))
        return out

    def delete_file(self, workspace_id: str, rel_path: str) -> None:
        workspace_id = self.registry.require(workspace_id)["workspace_id"]
        target = self._safe_join(workspace_id, rel_path)
        if not target.is_file():
            raise WorkspaceError(f"no such file in workspace: {rel_path!r}")
        target.unlink()

    # -- upload side channel (everything that is not .vast/.osc) ------------

    def create_upload(self, workspace_id: str, rel_path: str,
                      executable: bool = False) -> dict:
        """Issue a one-time, TTL-scoped grant for an HTTP PUT of *rel_path*."""
        workspace_id = self.registry.require(workspace_id)["workspace_id"]
        self._safe_join(workspace_id, rel_path)  # validate up front
        return self.tokens.issue(workspace_id, rel_path, executable=executable)

    def write_upload(self, token: str, data: bytes) -> dict:
        """Redeem *token* and write *data* into the workspace (any file type)."""
        grant = self.tokens.redeem(token)
        target = self._safe_join(grant["workspace_id"], grant["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if grant["executable"] or _has_shebang(data):
            # From here the existing _is_executable/_EXECUTABLE_META machinery
            # carries the bit through the object store and into the campaign.
            target.chmod(target.stat().st_mode | 0o111)
        return self._file_meta(target, grant["path"])

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
