#!/usr/bin/env python3
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

"""``vast share`` -- the external share, as its own command group.

The share is a separate system: its own storage, its own credentials, its own
lifetime. It used to be four verbs scattered through ``vast results``, where
``download`` meant either "ask the service" or "ask the share" depending on what
happened to be reachable -- and preferred the share even when a service was there.
Here the two are apart, and each verb names one transfer:

======================  =========================  ==========
verb                    moves                      who acts
======================  =========================  ==========
``list``                (reads the share)          you
``download``            share -> your disk         you
``upload``              your disk -> share         you
``remove``              (deletes on the share)     you
``export``              service -> share           the service
``import``              share -> service           the service
======================  =========================  ==========

**Who acts is decided by the endpoints, not by the direction.** The service acts when
a campaign in the service is one end of the transfer; you act when the two ends are
your disk and the share. That is what makes a read-only share credential a usable
one: ``list`` and ``download`` work with it, and ``upload``/``remove`` refuse,
because they are writes you are not allowed to make. ``remove`` deliberately does not
borrow the service's credentials to get around that -- doing so would let any
authenticated RoboVAST user delete share content the share itself would not let them
touch. Only ``export`` uses the service's write access, which is its whole purpose
and exactly what the campaign-end upload already does.
"""

import fnmatch
import os
import sys
import time
from pathlib import Path

import click

from robovast.client.errors import handle_cli_exception
from robovast.common import fmt_size as _fmt_size
from robovast.common import make_transfer_progress_callback
from robovast.execution.share_providers import load_share_provider_plugins
from robovast.execution.share_providers.naming import (VARIANTS, archive_name,
                                                       parse_archive_name)


@click.group()
def share():
    """Exchange campaigns with the configured external share.

    The share holds campaign archives named ``<campaign-id>.<variant>.tar.gz``, where
    the variant is ``raw`` (the campaign before postprocessing -- what a campaign
    launched with ``--upload-to-share`` puts there) or ``postprocessed``. Nobody is
    asked which: it is read off the campaign, and off the archive on the way back.

    Which share, and how to reach it, comes from the environment (a project ``.env``):

    \b
    ROBOVAST_SHARE_TYPE   — provider: gcs | webdav | nextcloud | sftp
    ROBOVAST_GCS_BUCKET   — bucket             (gcs)
    ROBOVAST_WEBDAV_URL   — collection URL     (webdav; plus _USER / _PASSWORD)
    ROBOVAST_SHARE_URL    — public share link  (nextcloud)
    ROBOVAST_SFTP_HOST    — host               (sftp; plus _USER / _REMOTE_DIR)

    Your credentials may be read-only, and that is a supported way to be set up:
    ``list`` and ``download`` work, ``upload`` and ``remove`` are refused by the share.
    ``export`` and ``import`` are done by the service with its own credentials.
    """


# ---------------------------------------------------------------------------
# Shared resolution
# ---------------------------------------------------------------------------

def _provider():
    """Instantiate the configured share provider, or raise a usable ``UsageError``.

    Every verb you perform yourself starts here, so the "what do I even put in
    ``.env``" answer is written once instead of once per command.
    """
    share_type = os.environ.get("ROBOVAST_SHARE_TYPE", "").strip()
    if not share_type:
        raise click.UsageError(
            "ROBOVAST_SHARE_TYPE is not set, so there is no share to talk to.\n"
            "Add it to a .env file in your project directory, e.g.:\n"
            "  ROBOVAST_SHARE_TYPE=gcs\n"
            "  ROBOVAST_GCS_BUCKET=my-robovast-results")
    providers = load_share_provider_plugins()
    if share_type not in providers:
        available = ", ".join(sorted(providers)) or "(none installed)"
        raise click.UsageError(
            f"Unknown share type '{share_type}'.\nAvailable providers: {available}")
    return share_type, providers[share_type]()


def _archives(provider):
    """``[(object_name, campaign_id, variant, size)]`` for everything on the share.

    Sorted by campaign id. A provider that cannot list at all raises, rather than
    reporting an empty share -- "nothing there" and "I cannot see" are different
    answers and only one of them means it is safe to re-upload.
    """
    try:
        listing = provider.list_campaign_archives_with_size()
    except NotImplementedError as exc:
        raise click.UsageError(str(exc)) from exc

    found = []
    for object_name, size in listing:
        parsed = parse_archive_name(os.path.basename(object_name))
        if parsed is None:
            continue
        campaign_id, variant = parsed
        found.append((object_name, campaign_id, variant, size))
    found.sort(key=lambda rec: rec[1])
    return found


def _select(archives, patterns, *, what="download"):
    """Filter *archives* by campaign-id patterns (globs allowed); raise if none match."""
    if not patterns:
        return archives
    selected = [rec for rec in archives
                if any(fnmatch.fnmatch(rec[1], pat) for pat in patterns)]
    if not selected:
        raise click.UsageError(
            f"None of the requested campaigns are on the share, so there is nothing to "
            f"{what}.\nRequested: {', '.join(sorted(patterns))}\n"
            "Run 'vast share list' to see what is there.")
    return selected


# ---------------------------------------------------------------------------
# Verbs you perform yourself
# ---------------------------------------------------------------------------

@share.command(name='list')
@click.argument('campaigns', nargs=-1)
def list_cmd(campaigns):
    """List the campaign archives on the share, with their variant and size.

    Pass one or more CAMPAIGNS (globs allowed) to narrow the listing.

    Each line also says whether that campaign is present in the reachable service.
    ``importable`` means it is not -- an archive whose campaign was cleaned up here, or
    that was produced somewhere else entirely, and the reason ``vast share import``
    exists. The share is not a subset of what the service has, so neither listing is
    authoritative for the other.
    """
    share_type, provider = _provider()
    click.echo(f"Listing campaigns on {share_type}...")
    try:
        archives = _select(_archives(provider), campaigns, what="list")
    except click.UsageError:
        raise
    except Exception as exc:  # noqa: BLE001 - rendered as a CLI error
        handle_cli_exception(exc)
        return

    if not archives:
        click.echo("No campaign archives found on the share.")
        return

    here = _campaign_ids_in_service()
    total = 0
    for _obj, campaign_id, variant, size in archives:
        size_str = _fmt_size(size) if size >= 0 else "unknown size"
        # `here is None` means no service answered, so "importable" would be a guess.
        where = "" if here is None else ("" if campaign_id in here else "  importable")
        click.echo(f"  {campaign_id}  [{variant}]  {size_str}{where}")
        if size >= 0:
            total += size

    click.echo()
    if any(size >= 0 for *_rest, size in archives):
        click.echo(f"  {len(archives)} archive(s)  total {_fmt_size(total)}")
    else:
        click.echo(f"  {len(archives)} archive(s)")


def _campaign_ids_in_service():
    """Campaign ids the reachable service knows, or ``None`` if none answered.

    ``None`` rather than an empty set on purpose: with no service there is nothing to
    compare against, and marking every archive "importable" would be an invention.
    """
    from robovast.client.service_target import \
        detected_service_url  # pylint: disable=import-outside-toplevel
    url = detected_service_url()
    if not url:
        return None
    try:
        from robovast.service.http_client import \
            RobovastClient  # pylint: disable=import-outside-toplevel
        from robovast.service.interface import \
            ListCampaignsRequest  # pylint: disable=import-outside-toplevel
        resp = RobovastClient(url).list_campaigns(ListCampaignsRequest(limit=1000))
        return {c.campaign_id for c in resp.campaigns}
    except Exception as exc:  # noqa: BLE001 - a listing aid, never the point of the call
        click.echo(f"  (could not ask the service what it has: {exc})", err=True)
        return None


@share.command(name='download')
@click.argument('campaigns', nargs=-1)
@click.option('--output', '-o', 'output', default=None, type=click.Path(file_okay=False),
              help='Directory to write the archives into [default: the current directory]')
@click.option('--force', '-f', is_flag=True,
              help='Overwrite an archive of the same name that is already here')
def download_cmd(campaigns, output, force):
    """Download campaign archives from the share to this machine.

    Writes ``<campaign-id>.<variant>.tar.gz`` and stops there -- the archive is yours,
    to keep, copy, or hand to ``vast results import``. Nothing is extracted and no
    results directory is touched.

    An interrupted transfer leaves a ``.part`` file and the next run resumes from it;
    share transfers are the ones that get interrupted, so this is where resume lives.
    """
    share_type, provider = _provider()
    out_dir = Path(output) if output else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    click.echo(f"Listing campaigns on {share_type}...")
    archives = _select(_archives(provider), campaigns)
    if not archives:
        click.echo("No campaign archives found on the share.")
        return

    downloaded = skipped = 0
    for object_name, campaign_id, variant, _size in archives:
        base = archive_name(campaign_id, variant)
        dest = out_dir / base
        if dest.exists() and not force:
            click.echo(f"  {base}  already here, skipping (use --force to re-download)")
            skipped += 1
            continue

        tmp_path = out_dir / f".{base}.part"
        resume_offset = tmp_path.stat().st_size if tmp_path.exists() else 0
        if resume_offset:
            click.echo(f"  {campaign_id}  resuming from {_fmt_size(resume_offset)}...")
        else:
            click.echo(f"  {campaign_id}  downloading [{variant}]...")

        start = time.monotonic()
        try:
            provider.download_archive(
                object_name, str(tmp_path),
                make_transfer_progress_callback(campaign_id, start),
                resume_offset=resume_offset)
        except NotImplementedError as exc:
            raise click.UsageError(str(exc)) from exc
        except (click.UsageError, click.ClickException):
            raise
        except Exception as exc:  # noqa: BLE001
            # The .part file is left where it is: the next run resumes from it.
            sys.stdout.write("\n")
            handle_cli_exception(exc)
            continue
        finally:
            sys.stdout.write("\n")
            sys.stdout.flush()

        os.replace(tmp_path, dest)
        click.echo(f"  {campaign_id}  ✓  {_fmt_size(dest.stat().st_size)} "
                   f"in {time.monotonic() - start:.0f}s  ->  {dest}")
        downloaded += 1

    click.echo()
    parts = [f"✓ Downloaded {downloaded} archive(s)"]
    if skipped:
        parts.append(f"{skipped} skipped")
    click.echo("  ".join(parts))


@share.command(name='upload')
@click.argument('archives', nargs=-1, required=True, type=click.Path(exists=True,
                                                                    dir_okay=False))
@click.option('--force', '-f', is_flag=True,
              help='Replace an archive of the same name that is already on the share')
def upload_cmd(archives, force):
    """Upload campaign archive files from this machine to the share.

    ARCHIVES are ``.tar.gz`` files as ``vast results download`` or ``vast share
    download`` produce them. The campaign id is read from the archive's single
    top-level directory and the variant from whether ``_execution/data.db`` is in it,
    so the object is named the way everything else on the share is named -- whatever
    the file happens to be called on your disk.

    This is a write, so it needs share credentials that may write. A read-only
    credential is refused by the share, which is what read-only means.
    """
    import tarfile  # pylint: disable=import-outside-toplevel

    share_type, provider = _provider()
    existing = {name for _o, name, _v, _s in _archives(provider)} if not force else set()

    uploaded = 0
    for path in archives:
        # A ClickException here (not a tar, or not one campaign) aborts the whole run
        # rather than being skipped: it means the file you named is not what you think,
        # and quietly uploading the others would hide that.
        campaign_id, variant = _read_archive_identity(tarfile, path)
        object_name = archive_name(campaign_id, variant)
        if campaign_id in existing:
            click.echo(f"  {campaign_id}  already on the share, skipping "
                       "(use --force to replace)")
            continue
        click.echo(f"  {campaign_id}  uploading [{variant}] as {object_name}...")
        start = time.monotonic()
        try:
            provider.upload_archive(
                path, object_name,
                progress_callback=make_transfer_progress_callback(campaign_id, start))
        except NotImplementedError as exc:
            raise click.UsageError(str(exc)) from exc
        except (click.UsageError, click.ClickException):
            raise
        except Exception as exc:  # noqa: BLE001
            sys.stdout.write("\n")
            handle_cli_exception(exc)
            continue
        finally:
            sys.stdout.write("\n")
            sys.stdout.flush()
        click.echo(f"  {campaign_id}  ✓ uploaded to {share_type}")
        uploaded += 1

    click.echo()
    click.echo(f"✓ Uploaded {uploaded} archive(s) to {share_type}.")


def _read_archive_identity(tarfile_mod, path):
    """``(campaign_id, variant)`` for the archive at *path*, read from its index.

    Both facts are in the tar's member list, so neither costs an extraction: the
    campaign id is the single top-level directory (an archive with more than one is
    not a campaign and is refused here rather than halfway through an upload), and
    the variant is whether postprocessing's ``_execution/data.db`` is a member.
    """
    from robovast.execution.share_providers.naming import (  # pylint: disable=import-outside-toplevel
        POSTPROCESSED, RAW)
    try:
        with tarfile_mod.open(path, "r:*") as tar:
            tops, has_db = set(), False
            for member in tar:
                head = member.name.split("/", 1)[0]
                if head not in (".", ""):
                    tops.add(head)
                if member.name.endswith("/_execution/data.db"):
                    has_db = True
    except tarfile_mod.TarError as exc:
        raise click.ClickException(f"Cannot read '{path}' as a tar archive: {exc}") from exc

    if len(tops) != 1:
        raise click.ClickException(
            f"'{path}' does not hold exactly one campaign (top-level entries: "
            f"{', '.join(sorted(tops)) or 'none'}). A campaign archive has one "
            "directory named after the campaign.")
    return tops.pop(), (POSTPROCESSED if has_db else RAW)


@share.command(name='remove')
@click.option('--campaign', '-i', 'campaigns', multiple=True, required=True,
              help='Campaign to remove (globs such as "nav-2026-03-09-*" are allowed). '
                   'Repeatable.')
@click.option('--variant', type=click.Choice(list(VARIANTS)), default=None,
              help='Remove only this variant. Without it, every variant of the named '
                   'campaign goes.')
@click.option('--yes', '-y', is_flag=True, help='Skip the confirmation prompt')
def remove_cmd(campaigns, variant, yes):
    """Permanently delete campaign archives from the share.

    A campaign can have both variants on the share at once -- that is what naming them
    apart is for -- and by default this removes all of them, because "delete this
    campaign from the share" is the usual thing to mean. ``--variant`` narrows it to one,
    which is the only way to drop a postprocessed copy while keeping the raw snapshot it
    was computed from. The raw one is the irreplaceable half: postprocessing can be run
    again, a recording cannot.

    A write, performed with your credentials -- not the service's. Borrowing the
    service's would let anyone who can reach RoboVAST delete share content the share
    itself would refuse them, so a read-only credential is refused here on purpose.
    """
    share_type, provider = _provider()
    click.echo(f"Listing campaigns on {share_type}...")
    all_archives = _archives(provider)

    matched = [rec for rec in all_archives
               if any(fnmatch.fnmatch(rec[1], pat) for pat in campaigns)
               and (variant is None or rec[2] == variant)]

    def _is_glob(pattern):
        return any(c in pattern for c in ("*", "?", "["))

    unmatched = [p for p in campaigns
                 if not any(fnmatch.fnmatch(rec[1], p) for rec in all_archives
                            if variant is None or rec[2] == variant)]
    exact = [p for p in unmatched if not _is_glob(p)]
    if exact:
        qualifier = f" as {variant}" if variant else ""
        raise click.UsageError(
            f"Campaign(s) not found on the share{qualifier}: {', '.join(sorted(exact))}\n"
            "Run 'vast share list' to see what is there.")
    for pattern in (p for p in unmatched if _is_glob(p)):
        click.echo(f"  Warning: no campaigns matched pattern '{pattern}'")

    if not matched:
        click.echo("No campaigns to remove.")
        return

    if not yes:
        click.echo()
        for _obj, campaign_id, variant, size in matched:
            size_str = f"  ({_fmt_size(size)})" if size >= 0 else ""
            click.echo(f"  {campaign_id}  [{variant}]{size_str}")
        click.echo()
        click.confirm(f"Remove {len(matched)} campaign archive(s) from {share_type}?",
                      abort=True)

    removed = 0
    for object_name, campaign_id, _variant, _size in matched:
        click.echo(f"  {campaign_id}  removing...")
        try:
            provider.remove_archive(object_name)
        except NotImplementedError as exc:
            raise click.UsageError(str(exc)) from exc
        except (click.UsageError, click.ClickException):
            raise
        except Exception as exc:  # noqa: BLE001
            handle_cli_exception(exc)
            continue
        click.echo(f"  {campaign_id}  ✓ removed")
        removed += 1

    click.echo()
    click.echo(f"✓ Removed {removed} campaign archive(s) from {share_type}.")


# ---------------------------------------------------------------------------
# Verbs the service performs
# ---------------------------------------------------------------------------

@share.command(name='export')
@click.option('--campaign', '-i', 'campaign_id', required=True,
              help='Campaign in the service to publish to the share')
def export_cmd(campaign_id):
    """Publish a campaign from the service to the share.

    The service does the transfer with its own credentials, so nothing passes through
    this machine and your share credentials are not involved. The variant is read off
    the campaign: one that has been postprocessed goes up as ``postprocessed``, one
    that has not as ``raw`` -- the same rule the campaign-end ``--upload-to-share``
    follows, which is why the two can never disagree.

    Long-running: it returns as soon as the upload is under way. Watch it with
    ``vast wait <campaign-id>``, or in the campaign view.
    """
    from robovast.client.service_target import \
        service_client  # pylint: disable=import-outside-toplevel
    from robovast.service.interface import \
        RunShareRequest  # pylint: disable=import-outside-toplevel

    with service_client(require_service=True) as (client, label):
        click.echo(f"Exporting {campaign_id} to the share via {label} ...")
        result = client.run_share(RunShareRequest(campaign_id=campaign_id))
    click.echo(("✓ " if result.ok else "✗ ") + (result.message or ""))
    if not result.ok:
        raise SystemExit(1)


@share.command(name='import')
@click.argument('campaigns', nargs=-1, required=True)
@click.option('--force', '-f', is_flag=True,
              help='Replace a campaign of the same id that the service already has')
@click.option('--rebuild-store', is_flag=True,
              help="Reconstruct campaign.db from the results tree (a corrupt store's "
                   "recovery)")
def import_cmd(campaigns, force, rebuild_store):
    """Take campaigns from the share into the service.

    The **service** downloads from the share -- the archive never touches this
    machine, which is the point: a campaign can be many gigabytes and your laptop is
    not on the path between two servers. When what arrives is a raw archive,
    postprocessing is chained automatically, so what you get back is a campaign with
    its metric tables, not a directory to remember to reprocess.

    Long-running: it returns as soon as the import is under way, and the campaign
    appears immediately in the campaign view at phase ``importing``. Watch it with
    ``vast wait <campaign-id>``.
    """
    from robovast.client.service_target import \
        service_client  # pylint: disable=import-outside-toplevel
    from robovast.service.interface import \
        ImportCampaignRequest  # pylint: disable=import-outside-toplevel

    started, failed = [], False
    with service_client(require_service=True) as (client, label):
        click.echo(f"Importing {len(campaigns)} campaign(s) from the share via {label} ...")
        for campaign_id in campaigns:
            try:
                ref = client.import_campaign(ImportCampaignRequest(
                    share_archive=campaign_id, force=force, rebuild_store=rebuild_store))
            except Exception as exc:  # noqa: BLE001 - one bad name must not skip the rest
                click.echo(f"  ✗ {campaign_id}: {exc}", err=True)
                failed = True
                continue
            click.echo(f"  ✓ {ref.campaign_id}" + (f"  ({ref.note})" if ref.note else ""))
            started.append(ref.campaign_id)

    if started:
        click.echo("\nWatch them with: " + "  ".join(f"vast wait {c}" for c in started))
    if failed:
        raise SystemExit(1)
