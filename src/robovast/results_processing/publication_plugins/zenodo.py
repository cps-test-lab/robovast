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

"""Zenodo publication plugin for RoboVAST.

Uploads artifact files (produced by preceding publication plugins, e.g. zip
archives) to a Zenodo deposition.  The deposition is **not** submitted or
published; files are only uploaded so the user can review and publish manually.

Dataset metadata from the ``.vast`` file is also pushed to the deposition.

Configuration example:

.. code-block:: yaml

   results_processing:
     publication:
       - zip:
           filename: my_dataset.zip
           destination: archives/
       - zenodo:
           ask: true
           record_id: 1234567

Credentials are read from the ``.env`` file adjacent to the ``.vast`` file
(or the project root)::

    ZENODO_ACCESS_TOKEN=your_access_token_here

Set ``sandbox: true`` to test against ``sandbox.zenodo.org`` instead of the
production instance.

See ``docs/zenodo.rst`` for instructions on creating an access token with the
``deposit:write`` scope.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml

from robovast.common.progress import make_download_progress_callback
from robovast.results_processing.publication_plugins.base import BasePublicationPlugin

_ZENODO_BASE = "https://zenodo.org/api"
_ZENODO_SANDBOX_BASE = "https://sandbox.zenodo.org/api"


def _base_url(sandbox: bool) -> str:
    return _ZENODO_SANDBOX_BASE if sandbox else _ZENODO_BASE


def _auth(token: str) -> Dict[str, str]:
    """Return the Authorization header for all API requests."""
    return {"Authorization": f"Bearer {token}"}


def _get_deposition(base: str, record_id: int, token: str) -> dict:
    """Fetch draft record from the InvenioRDM API."""
    resp = requests.get(
        f"{base}/records/{record_id}/draft",
        headers=_auth(token),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _delete_file(base: str, record_id: int, filename: str, token: str) -> None:
    """Delete a file from an InvenioRDM draft (required before re-uploading)."""
    resp = requests.delete(
        f"{base}/records/{record_id}/draft/files/{filename}",
        headers=_auth(token),
        timeout=30,
    )
    if not resp.ok and resp.status_code != 404:
        raise requests.HTTPError(
            f"{resp.status_code} {resp.reason}: {resp.text}", response=resp
        )


def _list_deposition_files(base: str, record_id: int, token: str) -> List[str]:
    """Return filenames already present in the draft."""
    resp = requests.get(
        f"{base}/records/{record_id}/draft/files",
        headers=_auth(token),
        timeout=30,
    )
    resp.raise_for_status()
    entries = resp.json().get("entries", [])
    return [e.get("key", "") for e in entries]


class _ProgressFile:
    """File-like wrapper that reports read progress via a callback.

    Using a file-like object (rather than a generator) lets ``requests``
    honour the ``Content-Length`` header and avoid chunked transfer encoding,
    which the Zenodo files API does not accept.
    """

    def __init__(self, path: Path, callback, file_size: int) -> None:
        self._fh = open(path, "rb")  # noqa: WPS515
        self._callback = callback
        self._file_size = file_size
        self._sent = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        if chunk:
            self._sent += len(chunk)
            self._callback(self._sent, self._file_size)
        return chunk

    def __len__(self) -> int:
        return self._file_size

    def close(self) -> None:
        self._fh.close()


def _upload_file(base: str, record_id: int, filename: str, file_path: Path, token: str) -> None:
    """Upload *file_path* using the InvenioRDM 3-step files API.

    Steps: initialise → upload content → commit.
    Uses a file-like progress wrapper so that ``requests`` sends a proper
    ``Content-Length`` header instead of chunked transfer encoding.
    """
    auth = _auth(token)

    # 1. Initialise the file entry
    init_resp = requests.post(
        f"{base}/records/{record_id}/draft/files",
        json=[{"key": filename}],
        headers={**auth, "Content-Type": "application/json"},
        timeout=30,
    )
    if not init_resp.ok:
        raise requests.HTTPError(
            f"{init_resp.status_code} {init_resp.reason}: {init_resp.text}",
            response=init_resp,
        )

    # 2. Upload content
    file_size = file_path.stat().st_size
    start = time.monotonic()
    progress_cb = make_download_progress_callback(filename, start)

    pf = _ProgressFile(file_path, progress_cb, file_size)
    try:
        content_resp = requests.put(
            f"{base}/records/{record_id}/draft/files/{filename}/content",
            data=pf,
            headers={
                **auth,
                "Content-Type": "application/octet-stream",
                "Content-Length": str(file_size),
            },
            timeout=None,
        )
    finally:
        pf.close()

    sys.stdout.write("\n")
    sys.stdout.flush()
    if not content_resp.ok:
        raise requests.HTTPError(
            f"{content_resp.status_code} {content_resp.reason}: {content_resp.text}",
            response=content_resp,
        )

    # 3. Commit
    commit_resp = requests.post(
        f"{base}/records/{record_id}/draft/files/{filename}/commit",
        headers=auth,
        timeout=30,
    )
    if not commit_resp.ok:
        raise requests.HTTPError(
            f"{commit_resp.status_code} {commit_resp.reason}: {commit_resp.text}",
            response=commit_resp,
        )


def _load_vast_data(vast_path: str) -> Dict[str, Any]:
    """Return the parsed .vast file as a dict, or empty dict on failure."""
    try:
        with open(vast_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # pylint: disable=broad-except
        return {}


def _create_deposition(base: str, token: str) -> dict:
    """Create a new empty Zenodo draft record and return its JSON response."""
    resp = requests.post(
        f"{base}/records",
        json={
            "metadata": {"resource_type": {"id": "dataset"}},
            "access": {"record": "public", "files": "public"},
            "files": {"enabled": True},
        },
        headers={**_auth(token), "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


_ZENODO_PROJECT_FILE = ".robovast_zenodo_project"


def _project_file_path(vast_file: Optional[str], config_dir: str) -> Path:
    base = Path(vast_file).parent if vast_file else Path(config_dir)
    return base / _ZENODO_PROJECT_FILE


def _load_cached_record_id(project_path: Path, sandbox: bool) -> Optional[int]:
    if not project_path.exists():
        return None
    try:
        data = json.loads(project_path.read_text(encoding="utf-8"))
        key = "sandbox" if sandbox else "production"
        value = data.get(key)
        return int(value) if value is not None else None
    except Exception:  # pylint: disable=broad-except
        return None


def _save_record_id(project_path: Path, sandbox: bool, record_id: int) -> None:
    data: Dict[str, Any] = {}
    if project_path.exists():
        try:
            data = json.loads(project_path.read_text(encoding="utf-8"))
        except Exception:  # pylint: disable=broad-except
            pass
    key = "sandbox" if sandbox else "production"
    data[key] = record_id
    project_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")

_SUPPORTED_CREATOR_TYPES = {"DataCollector"}


def _map_creator(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Map a .vast creator entry to Zenodo's InvenioRDM creator format.

    Supported .vast fields:

    * ``name``        → ``person_or_org.family_name`` / ``given_name``
                        (split on first ``", "``; "Family, Given" convention)
    * ``identifiers`` → ``person_or_org.identifiers`` with ``scheme: orcid``
                        when the value matches ``XXXX-XXXX-XXXX-XXXX``
    * ``type``        → validated (must be in ``_SUPPORTED_CREATOR_TYPES``);
                        not forwarded — affiliations and roles are managed on
                        Zenodo directly.
    * ``affiliation`` → not forwarded (managed on Zenodo directly).

    Raises:
        ValueError: if ``type`` is present but not in ``_SUPPORTED_CREATOR_TYPES``.
    """
    creator_type = entry.get("type")
    if creator_type is not None and str(creator_type) not in _SUPPORTED_CREATOR_TYPES:
        supported = ", ".join(sorted(_SUPPORTED_CREATOR_TYPES))
        raise ValueError(
            f"Unsupported creator type '{creator_type}'. "
            f"Supported: {supported}."
        )

    # Build person_or_org
    name_str = str(entry.get("name", "")).strip()
    if ", " in name_str:
        family, given = name_str.split(", ", 1)
        person_or_org: Dict[str, Any] = {
            "type": "personal",
            "family_name": family.strip(),
            "given_name": given.strip(),
        }
    else:
        person_or_org = {"type": "personal", "name": name_str}

    # ORCID identifiers
    ids = entry.get("identifiers")
    if ids is not None:
        candidates = [ids] if isinstance(ids, str) else list(ids)
        orcid_entries = [
            {"scheme": "orcid", "identifier": str(v).strip()}
            for v in candidates
            if _ORCID_RE.match(str(v).strip())
        ]
        if orcid_entries:
            person_or_org["identifiers"] = orcid_entries

    return {"person_or_org": person_or_org}


def _creator_names(creators: List[Any]) -> List[str]:
    """Extract a sorted list of name strings from a Zenodo creators list.

    Handles both old flat format ``{"name": "Family, Given"}`` and the new
    InvenioRDM nested format ``{"person_or_org": {"family_name": ..., "given_name": ...}}``.
    """
    names = []
    for c in creators:
        if not isinstance(c, dict):
            continue
        # New format
        pop = c.get("person_or_org") or {}
        if isinstance(pop, dict):
            if "family_name" in pop:
                names.append(f"{pop['family_name']}, {pop.get('given_name', '')}".strip(", "))
                continue
            if "name" in pop:
                names.append(str(pop["name"]))
                continue
        # Old flat format
        if "name" in c:
            names.append(str(c["name"]))
    return sorted(names)


def _html_to_plain(html: str) -> str:
    """Convert Zenodo's stored HTML description to plain text for comparison."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<p\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _confirm_field(field: str, local: str, remote: str, overwrite: Optional[bool]) -> bool:
    """Return True if the field should be updated on Zenodo.

    When *overwrite* is ``True`` updates silently; ``False`` skips silently;
    ``None`` prompts the user.  Always updates when *remote* is empty.
    """
    if not remote:
        return True
    if local == remote:
        return False
    if overwrite is True:
        return True
    if overwrite is False:
        return False
    # Prompt — show a short excerpt to avoid walls of text
    def _excerpt(s: str, n: int = 120) -> str:
        return s[:n] + "…" if len(s) > n else s

    print(f"\n  {field} on Zenodo differs from .vast file:")
    print(f"    Remote : {_excerpt(remote)}")
    print(f"    Local  : {_excerpt(local)}")
    try:
        answer = input(f"  Overwrite {field} on Zenodo? [y/N] ").strip().lower()
    except EOFError:
        answer = "n"
    return answer in ("y", "yes")


def _update_zenodo_metadata(
    base: str,
    record_id: int,
    token: str,
    vast_data: Dict[str, Any],
    current_deposition: Dict[str, Any],
    overwrite: Optional[bool] = None,
) -> List[str]:
    """Push relevant fields from the .vast ``metadata`` section to Zenodo.

    Compares each field against what is already on Zenodo and prompts the user
    before overwriting non-empty values (unless *overwrite* is set).

    Newlines in ``description`` are converted to ``<br>`` so they render
    correctly in the Zenodo web UI.

    Returns a list of field names that were updated.
    """
    meta = vast_data.get("metadata") or {}
    if not meta:
        return []

    current_meta: Dict[str, Any] = current_deposition.get("metadata") or {}

    # Start from existing Zenodo metadata so unconfirmed fields are preserved.
    # The InvenioRDM PUT replaces the entire metadata object.
    zenodo_meta: Dict[str, Any] = dict(current_meta)
    zenodo_meta.setdefault("resource_type", {"id": "dataset"})

    updated: List[str] = []

    # Title
    title = meta.get("title") or meta.get("name")
    if title:
        local = str(title).strip()
        remote = str(current_meta.get("title") or "").strip()
        if _confirm_field("title", local, remote, overwrite):
            zenodo_meta["title"] = local
            updated.append("title")

    # Version
    version_raw = meta.get("version")
    if version_raw is not None:
        local = str(version_raw).strip()
        if local:
            remote = str(current_meta.get("version") or "").strip()
            if _confirm_field("version", local, remote, overwrite):
                zenodo_meta["version"] = local
                updated.append("version")

    # Description — newlines → <br> for Zenodo web UI rendering.
    # Skip if the vast file has no description or an explicit null.
    description_raw = meta.get("description")
    if description_raw is not None:
        local_plain = str(description_raw).strip()
        if local_plain:
            local_html = local_plain.replace("\n", "<br>")
            remote_html = str(current_meta.get("description") or "")
            remote_plain = _html_to_plain(remote_html)
            if _confirm_field("description", local_plain, remote_plain, overwrite):
                zenodo_meta["description"] = local_html
                updated.append("description")

    # License → InvenioRDM "rights" field: list of {"id": "spdx-id"}
    license_raw = meta.get("license")
    if license_raw is not None:
        local = str(license_raw).strip().lower()
        if local:
            remote_rights = current_meta.get("rights") or []
            remote = ", ".join(
                r.get("id", "") for r in remote_rights if isinstance(r, dict)
            ).lower()
            if _confirm_field("license", local, remote, overwrite):
                zenodo_meta["rights"] = [{"id": local}]
                updated.append("license")

    # Keywords (no prompt — non-destructive list field)
    kw = meta.get("keywords")
    if kw:
        if isinstance(kw, list):
            zenodo_meta["keywords"] = [str(k) for k in kw]
        elif isinstance(kw, str):
            zenodo_meta["keywords"] = [k.strip() for k in kw.split(",") if k.strip()]

    # Creators — only prompt when names change; affiliation/role diffs are ignored
    creators = meta.get("creators")
    if creators and isinstance(creators, list):
        mapped = [_map_creator(c) for c in creators]
        local_names = json.dumps(_creator_names(mapped), ensure_ascii=False)
        remote_raw = current_meta.get("creators") or []
        remote_names = json.dumps(_creator_names(remote_raw), ensure_ascii=False) if remote_raw else ""
        if _confirm_field("creators", local_names, remote_names, overwrite):
            zenodo_meta["creators"] = mapped
            updated.append("creators")

    if not updated and "keywords" not in meta:
        return updated

    # PUT the full record body — InvenioRDM requires access + files sections.
    # Only send the writable subfields; the GET response may contain extra
    # read-only keys (links, order, entries list, etc.) that the API rejects.
    _access = current_deposition.get("access") or {}
    if not isinstance(_access, dict):
        _access = {}
    _files_cfg = current_deposition.get("files") or {}
    if not isinstance(_files_cfg, dict):
        _files_cfg = {}
    resp = requests.put(
        f"{base}/records/{record_id}/draft",
        json={
            "access": {
                "record": _access.get("record", "public"),
                "files": _access.get("files", "public"),
            },
            "files": {"enabled": _files_cfg.get("enabled", True)},
            "metadata": zenodo_meta,
        },
        headers={**_auth(token), "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return updated


class Zenodo(BasePublicationPlugin):
    """Upload artifact files to a Zenodo deposition.

    This is an *upload* plugin (``plugin_type = "upload"``): it consumes files
    produced by preceding packaging plugins and skipped when ``--skip-upload``
    is passed to ``vast results publish``.

    Collects files marked as artifacts by preceding publication plugins (e.g.
    zip archives created by the ``zip`` plugin) and uploads them to a Zenodo
    deposition via the `new files API
    <https://developers.zenodo.org/#quickstart-upload>`_.

    The deposition is **not** submitted or published.  After uploading, log in
    to Zenodo, review the files, and publish manually.

    Dataset metadata from the ``.vast`` file (title, description, keywords,
    creators) is also pushed to the deposition.

    Configuration example:

    .. code-block:: yaml

       results_processing:
         publication:
           - zip:
               filename: dataset.zip
               destination: archives/
           - zenodo:
               ask: true
               record_id: 1234567

    Required ``.env`` variable::

        ZENODO_ACCESS_TOKEN=your_access_token_here

    See ``docs/zenodo.rst`` for step-by-step instructions on creating a Zenodo
    access token restricted to the ``deposit:write`` scope.
    """

    plugin_type = "upload"

    def __call__(  # type: ignore[override]
        self,
        results_dir: str,
        config_dir: str,
        record_id: Optional[int] = None,
        sandbox: bool = False,
        overwrite: Optional[bool] = None,
        _artifacts: Optional[List[str]] = None,
        _vast_file: Optional[str] = None,
        **_kwargs,
    ) -> Tuple[bool, str, List[str]]:
        """Upload artifact files to a Zenodo deposition.

        Args:
            results_dir: Path to the results directory.
            config_dir: Directory containing the .vast config file.
            record_id: Zenodo deposition ID (integer).  When omitted, the
                plugin prompts to create a new deposition and prints the ID
                to add to the .vast file.
            sandbox: When ``True``, use ``sandbox.zenodo.org`` instead of the
                production Zenodo instance.  Useful for testing.  Defaults to
                ``False``.
            overwrite: Controls behaviour when a file with the same name
                already exists in the deposition.

                * ``None`` (default) – prompt the user interactively; default
                  answer is *yes* (overwrite).
                * ``True`` – silently overwrite (re-upload) existing files.
                * ``False`` – silently skip existing files.

                When running with ``--force`` on the CLI this is automatically
                set to ``True``.
            _artifacts: List of absolute file paths produced by preceding
                publication plugins.  Injected automatically by the
                publication runner.
            _vast_file: Absolute path to the resolved .vast file.  Injected
                automatically by the publication runner.  Used to read dataset
                metadata.

        Returns:
            Tuple of ``(success, message, uploaded_paths)``.
        """
        token = os.environ.get("ZENODO_ACCESS_TOKEN", "").strip()
        if not token:
            return (
                False,
                "ZENODO_ACCESS_TOKEN is not set.  Add it to your .env file.\n"
                "See docs/zenodo.rst for instructions.",
                [],
            )

        if not _artifacts:
            return True, "No artifacts to upload.", []

        base = _base_url(sandbox)

        # ------------------------------------------------------------------ #
        # Resolve record_id: config → project file → create new
        # ------------------------------------------------------------------ #
        instance = "sandbox.zenodo.org" if sandbox else "zenodo.org"
        project_path = _project_file_path(_vast_file, config_dir)

        if record_id is None:
            cached = _load_cached_record_id(project_path, sandbox)
            if cached is not None:
                print(f"Using cached Zenodo record_id {cached} from {project_path.name}")
                record_id = cached
            else:
                print(f"No record_id configured for Zenodo ({instance}).")
                try:
                    answer = input("Create a new deposition now? [Y/n] ").strip().lower()
                except EOFError:
                    answer = "y"
                if answer not in ("", "y", "yes"):
                    return True, "Zenodo upload skipped (no record_id).", []
                try:
                    deposition = _create_deposition(base, token)
                except requests.HTTPError as exc:
                    return False, f"Failed to create Zenodo deposition: {exc}", []
                except requests.RequestException as exc:
                    return False, f"Network error creating Zenodo deposition: {exc}", []
                record_id = deposition["id"]
                _save_record_id(project_path, sandbox, record_id)
                vast_hint = f"\n  {_vast_file}" if _vast_file else ""
                print(
                    f"\nCreated Zenodo deposition {record_id} on {instance}.\n"
                    f"Saved to {project_path}.\n"
                    f"To pin it permanently, add to the zenodo: section in your .vast file:{vast_hint}\n"
                    f"\n    record_id: {record_id}\n"
                )

        # ------------------------------------------------------------------ #
        # Fetch draft record
        # ------------------------------------------------------------------ #
        try:
            deposition = _get_deposition(base, record_id, token)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return (
                    False,
                    f"Zenodo record {record_id} has no draft. "
                    "It may already be published (published records cannot receive new files).",
                    [],
                )
            return False, f"Failed to fetch Zenodo record {record_id}: {exc}", []
        except requests.RequestException as exc:
            return False, f"Network error fetching record {record_id}: {exc}", []

        # ------------------------------------------------------------------ #
        # Get existing filenames in the deposition
        # ------------------------------------------------------------------ #
        try:
            existing = set(_list_deposition_files(base, record_id, token))
        except requests.HTTPError as exc:
            return False, f"Failed to list files for deposition {record_id}: {exc}", []

        # ------------------------------------------------------------------ #
        # Filter to files that actually exist on disk
        # ------------------------------------------------------------------ #
        artifact_paths = [Path(p) for p in _artifacts if Path(p).is_file()]
        if not artifact_paths:
            return True, "No artifact files found on disk.", []

        # ------------------------------------------------------------------ #
        # Upload
        # ------------------------------------------------------------------ #
        uploaded: List[str] = []
        skipped: List[str] = []
        errors: List[str] = []

        print(
            f"Uploading {len(artifact_paths)} file(s) to Zenodo deposition {record_id}"
            + (" [sandbox]" if sandbox else "")
        )

        for idx, file_path in enumerate(artifact_paths, 1):
            filename = file_path.name
            print(f"  [{idx}/{len(artifact_paths)}] {filename}")

            if filename in existing:
                eff_overwrite = overwrite
                if eff_overwrite is None:
                    try:
                        answer = input(
                            f"    File '{filename}' already exists in deposition. "
                            "Overwrite? [Y/n] "
                        ).strip().lower()
                    except EOFError:
                        answer = ""
                    eff_overwrite = answer in ("", "y", "yes")

                if not eff_overwrite:
                    print(f"    Skipped.")
                    skipped.append(filename)
                    continue

                # InvenioRDM does not allow re-initialising an existing key —
                # delete the old file before uploading the new one.
                try:
                    _delete_file(base, record_id, filename, token)
                except requests.HTTPError as exc:
                    print(f"    Error deleting existing file: {exc}", file=sys.stderr)
                    errors.append(f"{filename}: could not delete existing file: {exc}")
                    continue

            try:
                _upload_file(base, record_id, filename, file_path, token)
                uploaded.append(filename)
            except requests.HTTPError as exc:
                print(f"    Error: {exc}", file=sys.stderr)
                errors.append(f"{filename}: {exc}")
            except OSError as exc:
                print(f"    Error: {exc}", file=sys.stderr)
                errors.append(f"{filename}: {exc}")

        # ------------------------------------------------------------------ #
        # Update Zenodo metadata from .vast file
        # ------------------------------------------------------------------ #
        meta_msg = ""
        if _vast_file and os.path.isfile(_vast_file):
            try:
                vast_data = _load_vast_data(_vast_file)
                updated_fields = _update_zenodo_metadata(
                    base, record_id, token, vast_data, deposition, overwrite
                )
                if updated_fields:
                    meta_msg = f"metadata updated: {', '.join(updated_fields)}"
                else:
                    meta_msg = "metadata unchanged"
            except requests.HTTPError as exc:
                meta_msg = f"metadata update failed: {exc}"
            except Exception as exc:  # pylint: disable=broad-except
                meta_msg = f"metadata update failed: {exc}"

        # ------------------------------------------------------------------ #
        # Build summary
        # ------------------------------------------------------------------ #
        parts: List[str] = []
        if uploaded:
            parts.append(f"uploaded: {', '.join(uploaded)}")
        if skipped:
            parts.append(f"skipped (already exist): {', '.join(skipped)}")
        if meta_msg:
            parts.append(meta_msg)
        summary = " | ".join(parts) if parts else "done"

        if errors:
            err_detail = "; ".join(errors)
            return False, f"{summary} | errors: {err_detail}", [str(p) for p in artifact_paths]
        return True, summary, [str(p) for p in artifact_paths]
