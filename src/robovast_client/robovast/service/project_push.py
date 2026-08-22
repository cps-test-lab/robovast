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

"""Push a **local** project to a service workspace, then run it via the service.

This is how a CLI client with local project files (``vast exec cluster run``)
drives a remote/cluster ``robovast-service``: it uploads the project into a
server-side workspace (``.vast``/``.osc`` inline, everything else via the HTTP
PUT side channel, preserving executables), then calls ``create_campaign``.

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


def _is_project_input(rel: Path, main_vast: str) -> bool:
    """True if *rel* is a real project input to push.

    Excludes generated/cache/hidden directories, hidden files, and any ``.vast``
    other than the one being run (*main_vast*) — so generated/variation ``.vast``
    files don't violate the one-``.vast``-per-workspace rule.
    """
    if _is_generated(rel):
        return False
    if rel.suffix.lower() == ".vast" and rel.name != main_vast:
        return False
    return True


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
        resp.raise_for_status()
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


def push_project_files(client, workspace_id: str, config_path: str, *,
                       prune: bool = False, echo=None) -> dict:
    """Push the project rooted at *config_path*'s directory into an existing workspace.

    ``.vast``/``.osc`` files go inline; everything else (run files, notebooks,
    binaries) streams through the PUT side channel with the executable bit
    preserved. With *prune*, workspace files absent from the project are deleted, so
    the workspace mirrors the directory — what a *launch* wants, since a stale run
    file left from an earlier push would be copied into the campaign.

    Deliberately not :func:`sync_directory_to_workspace`: the predicate here is
    :func:`_is_project_input`, which additionally drops ``__pycache__``, ``_config/``,
    ``_transient/`` and every ``.vast`` other than the one being run — the last of
    which is what keeps the one-``.vast``-per-workspace rule.

    Prune covers everything :func:`_is_generated` does *not* claim — so a stale run file
    goes, and so does a ``.vast`` renamed since the last push (which would otherwise
    survive as a second one and make the workspace unlaunchable), while the service's
    own ``.cache/`` and staged plugins are left where they are.

    Returns ``{"written", "uploaded", "pruned"}`` counts.
    """
    config_path = Path(config_path).resolve()
    project_dir = config_path.parent
    main_vast = config_path.name
    stats = {"written": 0, "uploaded": 0, "pruned": 0}
    local_rels: set[str] = set()

    # The content-based half of `_is_generated` needs an absolute root to stat against.
    # try/finally, not a bare assignment: leaving it set would make the NEXT push resolve
    # its relative paths against this project's directory.
    global _content_root  # pylint: disable=global-statement
    _content_root = project_dir
    try:
        files, skipped = collect_inputs(project_dir, skip_dirs=_SKIP_DIRS)
        for rel_path in files:
            if not _is_project_input(rel_path, main_vast):
                continue
            rel_str = rel_path.as_posix()
            kind = push_file(client, format_address(SOURCES, workspace_id, rel_str),
                             project_dir / rel_path)
            stats["written" if kind == "written" else "uploaded"] += 1
            local_rels.add(rel_str)
            if echo:
                echo(f"  + {rel_str}")
        if echo and skipped:
            report_skipped(skipped, echo)
    finally:
        _content_root = None

    if prune:
        existing = client.list_files(format_address(SOURCES, workspace_id),
                                     recursive=True, limit=0).entries
        for rel_str in sorted(existing):
            if rel_str in local_rels or _is_generated(Path(rel_str)):
                continue
            client.delete_file(format_address(SOURCES, workspace_id, rel_str))
            stats["pruned"] += 1
            if echo:
                echo(f"  - {rel_str} (pruned)")

    logger.info("Pushed project %s into workspace %s (%s)",
                project_dir, workspace_id, stats)
    return stats


def push_project_to_workspace(client, config_path: str, name: str = "") -> str:
    """Upload the project rooted at *config_path*'s directory into a **new** workspace.

    Returns the new ``workspace_id``. To reuse the workspace a project already has,
    see :func:`workspace_for_project`.
    """
    from robovast.service.interface import CreateWorkspaceRequest

    project_dir = Path(config_path).resolve().parent
    workspace_id = client.create_workspace(
        CreateWorkspaceRequest(name=name or project_dir.name)).workspace_id
    push_project_files(client, workspace_id, config_path)
    return workspace_id


def workspace_for_project(client, config_path: str, name: str = "",
                          *, on_exists=None) -> tuple[str, str]:
    """The workspace to push *config_path*'s project into; ``(workspace_id, action)``.

    Named after the project directory (or *name*), and **reused** when it is already
    there: launching the same project twice must not leave ``myproj``, ``myproj-2``,
    ``myproj-3`` behind — the server auto-suffixes a colliding name, so creating
    unconditionally accumulates one workspace per launch.

    *on_exists* ``(name, workspace_id) -> bool`` is asked before an existing workspace
    is reused, since reusing it overwrites its files. A false answer raises, so nothing
    is pushed or launched. Absent, the workspace is reused.

    ``action`` is ``"created"`` or ``"reused"``, for the caller to report.
    """
    from robovast.service.interface import CreateWorkspaceRequest

    project_dir = Path(config_path).resolve().parent
    wanted = name or project_dir.name

    matches = [w for w in client.list_workspaces().workspaces if w.name == wanted]
    if len(matches) > 1:
        # The registry auto-suffixes, so same-named rows only exist if they were made
        # by hand. Refuse rather than pick one and overwrite the wrong project.
        ids = ", ".join(w.workspace_id for w in matches)
        raise ValueError(
            f"{len(matches)} workspaces are named {wanted!r} ({ids}); "
            "pass an explicit workspace name")
    if matches:
        workspace_id = matches[0].workspace_id
        # Before the prompt, because this one is not the caller's to wave through: a
        # campaign reads its project out of the workspace for its whole life, so a push
        # now would change an experiment that is still running. Refuse and name it.
        running = list(getattr(matches[0], "running_campaigns", None) or [])
        if running:
            raise ValueError(
                f"workspace {wanted!r} ({workspace_id}) is being read by "
                f"{', '.join(running)} — pushing to it now would change a running "
                "campaign's project. Wait for it, stop it, or launch into another "
                "workspace")
        if on_exists is not None and not on_exists(wanted, workspace_id):
            raise ValueError(f"declined to overwrite workspace {wanted!r} ({workspace_id})")
        return workspace_id, "reused"

    created = client.create_workspace(CreateWorkspaceRequest(name=wanted))
    return created.workspace_id, "created"


def run_project_via_service(client, config_path: str,
                            config_filter: str = "", runs: int = 0,
                            feedback=None, upload_to_share: bool = False,
                            campaign_name: str = "", description: str = "",
                            workspace_name: str = "", on_exists=None,
                            image_project: str | None = None,
                            image_project_tag: str | None = None,
                            allow_opaque_image: bool = False) -> str:
    """Push the local project through *client* and start a campaign. Returns id.

    ``runs=0`` means "whatever the ``.vast`` declares": the service maps a non-positive
    count to ``None`` and falls back to ``execution.runs``. Any other substitute for
    "unset" is an override nobody asked for, and it shrinks the campaign silently —
    fewer repetitions is not a failure any later stage can notice.

    ``image_project`` / ``image_project_tag`` are ``None`` for "whatever the environment
    says", which is read *here* rather than left to the service: the client's ``.env`` is
    where an operator configures their own registry, and the service cannot see it.
    """
    from robovast.service.interface import CreateCampaignRequest

    say = feedback or logger.info
    if image_project is None:
        image_project = os.environ.get("ROBOVAST_PROJECT", "").strip()
    if image_project_tag is None:
        image_project_tag = os.environ.get("ROBOVAST_PROJECT_TAG", "").strip()
    workspace_id, action = workspace_for_project(
        client, config_path, workspace_name, on_exists=on_exists)
    say(f"Pushing project to robovast-service ({action} workspace {workspace_id}) ...")
    push_project_files(client, workspace_id, config_path, prune=True)
    if image_project or image_project_tag:
        # Said out loud: which images a campaign ran against is the first thing anyone
        # asks when a result looks wrong, and this is a per-launch override that leaves
        # no trace in the .vast.
        say(f"RoboVAST images for this campaign: "
            f"{image_project or '(service default)'}/*:"
            f"{image_project_tag or '(service default)'}")
    say(f"Uploaded to workspace {workspace_id}; starting campaign ...")
    ref = client.create_campaign(CreateCampaignRequest(
        workspace_id=workspace_id, config_filter=config_filter,
        campaign_name=campaign_name, description=description,
        runs=runs if runs and runs > 0 else 0,
        upload_to_share=upload_to_share, allow_opaque_image=allow_opaque_image,
        image_project=image_project, image_project_tag=image_project_tag))
    return ref.campaign_id


def download_campaign_archive(client, campaign_id: str, dest_path: str,
                              progress_callback=None) -> str:
    """Stream the campaign's ``tar.gz`` through *client* into *dest_path*; return it.

    A file lands, and that is all that happens. This used to stream-*extract* off the
    socket, which made "download" also decide where a results tree goes and what it is
    called -- two jobs, and the second one nobody asked for. Unpacking is ``tar``'s, and
    putting a campaign back into a service is ``vast results import``.

    Written through a ``.part`` sibling and renamed on success, so an interrupted
    transfer cannot leave a truncated archive sitting under the real name looking
    complete. There is no resume: this is a service on your own network, and a
    half-finished HTTP GET is cheaper to repeat than to reason about.
    """
    from robovast.service.interface import Routes  # pylint: disable=import-outside-toplevel

    say = logger.info
    url = f"{client.base_url}{Routes.CAMPAIGNS}/{campaign_id}/archive"
    say("Downloading %s from robovast-service ...", campaign_id)
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or ".", exist_ok=True)
    tmp_path = f"{dest_path}.part"
    try:
        with client.session.get(url, timeout=600, stream=True) as resp:
            resp.raise_for_status()
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
