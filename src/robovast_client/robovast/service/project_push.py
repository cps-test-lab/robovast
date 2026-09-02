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

"""Move a **local** directory into a service workspace, and back out again.

A service cannot read the caller's disk, so getting files to it is the one step that has
to happen client-side: ``.vast``/``.osc`` go inline, everything else through the HTTP PUT
side channel with the executable bit preserved. ``vast workspace init``/``update`` push,
``vast workspace download`` pulls, and ``vast files put``/``get`` do one file.

Nothing here launches anything. A campaign runs a *workspace's* project -- a workspace id
and a path within it -- so the launch is one ``create_campaign`` call that needs none of
this, and ``vast workspace run`` makes it directly. No push-then-launch path lives here,
whose whole job would be reducing a local file path to the workspace the service already
wanted, nor a rule pruning every ``.vast`` but one so a launch could leave ``config_path``
empty: a workspace holds as many projects as it likes, and the path names which one.

Reused by the CLI; the LocalTransport/HTTP client itself stays transport-agnostic.
"""

import contextlib
import logging
import os
from pathlib import Path

from robovast.client.file_address import SOURCES, format_address
from robovast.client.workspaces import is_campaign_results_dir
from robovast.client.workspaces import is_skipped as _should_skip

logger = logging.getLogger(__name__)

#: Absolute root the content-based results check resolves against, set for the duration
#: of one push. Module-level because ``_is_generated`` is called through a predicate
#: signature that takes only a relative path, and threading a root through every caller
#: would change three public functions to fix one of them.
_content_root: Path | None = None

_INLINE_EXTS = (".vast", ".osc")

# Generated/cache artefacts that must not be pushed as project inputs. ``results`` is
# here for the same reason ``vast workspace init`` excludes it: it is a campaign's
# *output*, and pushing it uploads every past campaign on disk as project input on every
# launch.
#
# THE NAME IS NO LONGER THE ONLY DEFENCE, and it never could be: a project whose
# ``.vast_project`` names a different results dir, or one holding a campaign downloaded
# under its own id, is not on this list and cannot be -- the name is not knowable from
# the ``.vast``. ``_is_generated`` therefore also asks whether a directory *contains* a
# campaign's markers (``is_campaign_results_dir``), which is knowable from the directory
# itself and stays true when a naming convention changes.
_SKIP_DIRS = {".cache", ".preprocessed", "resolved", "_execution", "_transient",
              "_config", "_control", "_jobs", "__pycache__", ".git", "results"}


def _is_generated(rel: Path) -> bool:
    """True if *rel* is a generated/cache/hidden artefact rather than authored input.

    The same predicate on both sides of a push: locally these are build leftovers not
    worth uploading, and **inside a workspace they belong to the service** — a campaign
    writes ``.cache/`` (config generation), ``.robovast_plugins/`` and ``resolved/``
    into the project dir it runs from. So a mirroring push must neither send them nor
    delete them; pruning the service's own cache forces a full regeneration on every
    relaunch, and does it while a campaign may still be reading it.
    """
    if any(p in _SKIP_DIRS or p.startswith(".") for p in rel.parts):
        return True
    # And the same question by content, for a results tree this list cannot name. Asked
    # of each ANCESTOR directory rather than of the file: the markers sit at the results
    # root, so `<campaign-id>/goal-1/0/poses.csv` is only recognisable from
    # `<campaign-id>/`. Relative to the caller's root via `_content_root`, which is set
    # for the duration of one push -- the predicate needs an absolute path to stat.
    root = _content_root
    if root is None:
        return False
    parent = rel.parent
    while parent != Path("."):
        if is_campaign_results_dir(root / parent):
            return True
        parent = parent.parent
    return False


def push_file(client, address: str, path: Path) -> str:
    """Push one local file to a ``/sources`` *address*. Returns ``"written"``/``"uploaded"``.

    ``.vast``/``.osc`` go inline (last-write-wins, so this both creates and
    overwrites); everything else streams through the PUT side channel with the
    executable bit preserved. The one place that knows about both transports: the
    HTTP client issues an absolute PUT URL (``grant.url``), the in-process
    ``LocalTransport`` exposes ``client.store`` for a direct write.

    Public and address-taking because ``vast files put`` needs exactly this and
    should not have to reach for a private helper or re-derive the address itself.
    """
    from robovast.service.interface import CreateUploadRequest, WriteFileRequest

    if path.suffix.lower() in _INLINE_EXTS:
        client.write_file(WriteFileRequest(
            address=address, content=path.read_text(encoding="utf-8")))
        return "written"

    grant = client.create_upload(CreateUploadRequest(
        address=address, executable=os.access(path, os.X_OK)))
    data = path.read_bytes()
    if grant.url:  # HTTP service issued an absolute PUT URL
        # The client's own session, not a bare requests.put: the upload route is
        # behind the same authentication as everything else, and a fresh request
        # would carry no credentials.
        client.session.put(grant.url, data=data, timeout=120).raise_for_status()
    elif hasattr(client, "store"):  # in-process LocalTransport
        client.store.write_upload(grant.token, data)
    else:
        raise RuntimeError(
            f"cannot upload {address!r}: this client has no upload channel")
    return "uploaded"


def push_campaign_archive(client, path: Path) -> str:
    """Stream a campaign archive to the service and return where it landed there.

    The archive counterpart of :func:`push_file`, and the same "one place knows about both
    transports" job: an HTTP service issues an absolute PUT URL, while an in-process
    transport already shares this filesystem and so needs no transfer at all -- its own
    path *is* the staged path.

    Returns a path on the service host, for :meth:`import_campaign` to import. Streamed
    from disk rather than read into memory: a campaign archive is routinely gigabytes, and
    this runs on a laptop.
    """
    grant = client.create_archive_upload()
    if grant.url:
        with open(path, "rb") as fh:
            # The client's own session, for the reason push_file gives: the route is behind
            # the same authentication as everything else. `data=` a file object streams it.
            resp = client.session.put(grant.url, data=fh, timeout=None)
        # The client's helper: an expired or replayed grant answers with a sentence saying
        # so, and requests' own would report only "404 for url ...".
        client.raise_for_status(resp)
        return resp.json()["path"]
    return str(path)


def _resolve_workspace_id(client, ref: str) -> str:
    """Resolve a workspace id-or-name to a concrete ``workspace_id``.

    A ``ws-…`` id is returned as-is; anything else is matched by name against the
    service's workspaces. Fails loudly on no match or an ambiguous name, so a typo
    never silently targets the wrong workspace.
    """
    if ref.startswith("ws-"):
        return ref
    matches = [w for w in client.list_workspaces().workspaces if w.name == ref]
    if not matches:
        raise ValueError(f"no workspace named {ref!r}")
    if len(matches) > 1:
        raise ValueError(
            f"workspace name {ref!r} is ambiguous ({len(matches)} matches); "
            "use the ws-… id")
    return matches[0].workspace_id


def pull_workspace_to_directory(client, workspace_id: str, directory, *,
                                 overwrite: bool = False, echo=None) -> dict:
    """Fetch every file in *workspace_id* into a local *directory*. Returns counts.

    The other direction of :func:`sync_directory_to_workspace`, and deliberately built on the
    existing per-file calls rather than on a new archive endpoint. A workspace is a *source*
    project -- a ``.vast``, a scenario, some params -- so ``list_files`` plus ``read_file_bytes``
    is adequate, and adding a service route for a convenience the client can already assemble
    would be surface nobody needs to maintain. A campaign is the opposite case: it holds rosbags,
    which is why *that* transfer is an archive.

    The executable bit is carried back, because it is carried *out* by ``push_file`` and a run
    script that arrives non-executable fails at the point of use rather than here.

    Raises:
        FileExistsError: a file is already present and *overwrite* is false. Refused per-file
            rather than checked once for the directory: pulling into a directory that holds an
            edited copy of the same project is the likely mistake, and silently overwriting
            somebody's local edits is not recoverable.
    """
    target_root = Path(directory)
    target_root.mkdir(parents=True, exist_ok=True)
    listing = client.list_files(format_address(SOURCES, workspace_id, ""),
                                recursive=True, detail=True, limit=100000)

    # `detailed`, not `entries`: with detail=True the objects land in their own field and
    # `entries` stays empty, while `total` still counts them -- so reading `entries` here looked
    # like an empty workspace rather than a wrong field name.
    counts = {"fetched": 0, "skipped": 0}
    for entry in listing.detailed:
        if getattr(entry, "is_dir", False):
            continue
        target = target_root / entry.name
        if target.exists() and not overwrite:
            raise FileExistsError(
                f"{target} already exists. Pulling would overwrite a local copy that may hold "
                f"edits this workspace does not have; pass overwrite to replace it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(client.read_file_bytes(
            format_address(SOURCES, workspace_id, entry.name)))
        if getattr(entry, "executable", None):
            target.chmod(target.stat().st_mode | 0o111)
        counts["fetched"] += 1
        if echo:
            echo(f"  + {entry.name}")
    return counts


def _dir_size(path: Path) -> int:
    """Total bytes under *path*, best-effort. Only for the skip report."""
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:      # a vanishing temp file must not break a report
            continue
    return total


def _human(size: int) -> str:
    """*size* as a short human-readable string."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"


def collect_inputs(root: Path, *, skip_dirs=frozenset(),
                   include_results: bool = False) -> tuple[list[Path], list[tuple]]:
    """The files to push under *root*, and the directories deliberately not pushed.

    Walks **top-down and prunes**, rather than listing everything and filtering after.
    That is not only faster: ``rglob("*")`` descends into a skipped tree before deciding
    to drop it, so a project sitting beside a multi-gigabyte results directory paid for
    stat-ing every file in it in order to ignore them all.

    Returns ``(files, skipped)``, where each *skipped* entry is
    ``(rel_posix, reason, bytes)`` -- carried out rather than logged here so the caller
    can report it in its own voice, and so a caller that wants the bytes anyway
    (*include_results*) can say so.
    """
    files: list[Path] = []
    skipped: list[tuple] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        keep = []
        for name in sorted(dirnames):
            # Hidden dirs are dropped in silence: they are caches and VCS metadata, they
            # were never project input, and reporting them would bury the report that
            # matters under `.git`, `.cache`, `.venv` on every push.
            if name.startswith("."):
                continue
            rel = (here / name).relative_to(root)
            if name in skip_dirs:
                skipped.append((rel.as_posix(), "excluded by name", _dir_size(here / name)))
                continue
            if not include_results and is_campaign_results_dir(here / name):
                skipped.append((rel.as_posix(), "campaign results",
                                _dir_size(here / name)))
                continue
            keep.append(name)
        dirnames[:] = keep          # in place: this is what prunes the walk
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            files.append((here / name).relative_to(root))
    return sorted(files), skipped


def report_skipped(skipped: list[tuple], echo) -> None:
    """Say what was not uploaded, and how to upload it anyway.

    **Reported rather than assumed**, because the alternative is a filter that quietly
    decides what an author meant. A skipped directory is usually right and occasionally
    wrong -- a project really can keep a hand-authored file inside a directory that looks
    like campaign output -- and the only way to tell is to say so and name the flag that
    reverses it.
    """
    total = sum(size for _, _, size in skipped)
    echo(f"  skipped {len(skipped)} director{'y' if len(skipped) == 1 else 'ies'} "
         f"({_human(total)} not uploaded):")
    width = max(len(rel) for rel, _, _ in skipped)
    for rel, reason, size in sorted(skipped, key=lambda e: -e[2]):
        echo(f"    {rel:<{width}}  {reason:<17} {_human(size):>9}")
    if any(reason == "campaign results" for _, reason, _ in skipped):
        echo("  campaign results are a campaign's OUTPUT, not project input; "
             "pass --include-results to upload them anyway")


def require_not_in_use(client, workspace_id: str) -> None:
    """Refuse a bulk sync into a workspace a campaign is currently reading.

    A campaign reads its project out of the workspace for its whole life -- a search
    campaign re-composes from it every generation -- so overwriting one mid-run changes an
    experiment underneath itself. ``WorkspaceInfo.running_campaigns`` is what answers this:
    live state held by the service driving the run, never a stored campaign->workspace
    binding, because a *finished* campaign is workspace-independent.

    Not the caller's to wave through, so this is a refusal rather than a prompt -- unlike
    the overwrite question, which only risks the caller's own files.

    Asked of ``list_workspaces`` rather than of a dedicated endpoint so it works
    identically for an in-process transport and an HTTP client.
    """
    match = next((w for w in client.list_workspaces().workspaces
                  if w.workspace_id == workspace_id), None)
    if match is None:
        return
    running = list(getattr(match, "running_campaigns", None) or [])
    if running:
        raise ValueError(
            f"workspace {match.name!r} ({workspace_id}) is being read by "
            f"{', '.join(running)} — pushing to it now would change a running "
            "campaign's project. Wait for it, stop it, or push to another workspace")


def sync_directory_to_workspace(client, workspace_id: str, directory, *,
                                skip_dirs=frozenset(), prune: bool = False,
                                include_results: bool = False, echo=None) -> dict:
    """Re-sync a local *directory* into an **existing** workspace.

    Uploads every non-hidden file under *directory* (``.vast``/``.osc`` inline,
    the rest via the PUT side channel), overwriting in place. Hidden files/dirs,
    any directory named in *skip_dirs*, and any **campaign results tree** (recognised
    by content -- see :func:`~robovast.client.workspaces.is_campaign_results_dir`) are
    skipped; *include_results* uploads the last of those anyway. With *prune*, workspace
    files absent from *directory* are deleted (full mirror). *echo* (e.g.
    ``click.echo``) receives one ``+``/``-`` line per change, and a closing report of
    every directory that was skipped. Returns ``{"written", "uploaded", "pruned",
    "skipped_dirs"}`` counts.

    Raises:
        FileNotFoundError: *directory* does not exist. Checked rather than left to
            ``rglob``, which yields nothing for a missing path: the sync then reported a
            cheerful ``{"written": 0, "uploaded": 0}`` for a push that pushed nothing,
            and with *prune* it went further and deleted every file in the workspace,
            because "no local files" and "the path is wrong" were indistinguishable. A
            typo was enough. This is the likeliest mistake of all against a remote
            service, where the directory is read on the service host and a path from the
            caller's machine is *expected* to be absent.
    """
    # A pinned directory is editable file by file but never mirrored wholesale; see
    # WorkspaceRegistry.require_syncable. Asked of the store when there is one -- an HTTP
    # client has no registry, and the service refuses on its own side.
    store = getattr(client, "store", None)
    if store is not None and hasattr(store, "registry"):
        store.registry.require_syncable(workspace_id)

    require_not_in_use(client, workspace_id)

    root = Path(directory).resolve()
    if not root.is_dir():
        raise FileNotFoundError(
            f"no such directory on the service host: {root}. This path is read where "
            "the service runs, not where you are -- if the service is remote, put the "
            "project in a workspace instead (vast workspace init <dir>).")
    stats = {"written": 0, "uploaded": 0, "pruned": 0}
    local_rels: set[str] = set()
    files, skipped = collect_inputs(root, skip_dirs=skip_dirs,
                                    include_results=include_results)
    for rel in files:
        rel_str = rel.as_posix()
        kind = push_file(client, format_address(SOURCES, workspace_id, rel_str),
                         root / rel)
        stats["written" if kind == "written" else "uploaded"] += 1
        local_rels.add(rel_str)
        if echo:
            echo(f"  + {rel_str}")
    stats["skipped_dirs"] = len(skipped)
    if echo and skipped:
        report_skipped(skipped, echo)

    if prune:
        existing = client.list_files(format_address(SOURCES, workspace_id),
                                     recursive=True, limit=0).entries
        for rel_str in sorted(existing):
            if rel_str in local_rels or _should_skip(Path(rel_str), skip_dirs):
                continue
            client.delete_file(format_address(SOURCES, workspace_id, rel_str))
            stats["pruned"] += 1
            if echo:
                echo(f"  - {rel_str} (pruned)")
    logger.info("Synced %s into workspace %s (%s)", root, workspace_id, stats)
    return stats


def _served_filename(disposition) -> str:
    """The bare file name out of a ``Content-Disposition`` header, or ``""``.

    Only a name is accepted: a header is remote input and this value becomes a path on the
    caller's disk, so anything carrying a separator or climbing out of the directory is
    dropped and the caller's own name stands.
    """
    if not disposition:
        return ""
    for part in disposition.split(";"):
        key, _, value = part.strip().partition("=")
        if key.strip().lower() != "filename":
            continue
        name = value.strip().strip('"')
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            return ""
        return name
    return ""


def download_campaign_archive(client, campaign_id: str, dest_path: str,
                              progress_callback=None) -> str:
    """Stream the campaign's ``tar.gz`` through *client* into *dest_path*; return it.

    A file lands, and that is all that happens. Stream-*extracting* off the socket would
    make "download" also decide where a results tree goes and what it is called -- two
    jobs, and the second one nobody asked for. Unpacking is ``tar``'s, and putting a
    campaign back into a service is ``vast campaign import``.

    Written through a ``.part`` sibling and renamed on success, so an interrupted
    transfer cannot leave a truncated archive sitting under the real name looking
    complete. There is no resume: this is a service on your own network, and a
    half-finished HTTP GET is cheaper to repeat than to reason about.

    The **service** names the file, not *dest_path*: a campaign that was still running when
    it was archived comes back as ``<id>.incomplete.tar.gz``, and only the service knows
    that. *dest_path* supplies the directory and the fallback name; the returned path is
    where the archive actually landed, which is the one a caller must report.
    """
    from robovast.service.interface import Routes  # pylint: disable=import-outside-toplevel

    say = logger.info
    url = f"{client.base_url}{Routes.CAMPAIGNS}/{campaign_id}/archive"
    say("Downloading %s from robovast-service ...", campaign_id)
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or ".", exist_ok=True)
    tmp_path = f"{dest_path}.part"
    try:
        with client.session.get(url, timeout=600, stream=True) as resp:
            # The client's helper, not requests' own: that one reports the status line and
            # the URL and throws the body away, which is where this service writes the
            # actionable sentence ("no campaign 'x' on this service").
            client.raise_for_status(resp)
            served = _served_filename(resp.headers.get("Content-Disposition"))
            if served:
                dest_path = os.path.join(os.path.dirname(os.path.abspath(dest_path)), served)
            # Absent for a campaign archive: the service tars it on the fly, so there is
            # nothing to divide by and the progress callback reports a running count.
            total = int(resp.headers.get("Content-Length") or 0)
            received = 0
            with open(tmp_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    received += len(chunk)
                    if progress_callback is not None:
                        progress_callback(received, total)
    except BaseException:
        # No resume on this path, so a partial file is litter rather than progress -- and
        # litter named after a campaign, in the directory the next attempt writes to.
        # BaseException so a Ctrl+C cleans up too.
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    os.replace(tmp_path, dest_path)
    return dest_path
