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

"""Build a campaign ``tar.gz`` from a local directory — as a file, or streamed.

One place produces the campaign archive for both directions of "share":

* :func:`make_campaign_tarball` writes ``<archive_dir>/<campaign>.tar.gz`` — the
  local backend's upload-to-share deliverable (there is no external provider
  locally, so the file *is* the artifact).
* :func:`campaign_tar_stream` / :func:`iter_campaign_tar` produce the same archive
  as an on-the-fly ``pigz`` stream with **no tar on disk** — used to push a
  campaign to an external share provider (upload-to-share, cluster) and to serve
  the ``/campaigns/{id}/archive`` download, both of which run against ~1TB
  campaigns where materialising a compressed copy would blow the pod's scratch.

Both read a **local directory**: upload-to-share streams the campaign already on
the driver's scratch; the download streams the dir ``fetch_campaign`` materialises
from the object store. Symlinks (the ``<config>/<run>/job`` links) are preserved as
symlink members (``dereference=False``) and not recursed into, so the archive is
navigable without duplicating ``_jobs/`` under every run.
"""

import contextlib
import logging
import os
import subprocess  # nosec B404 - fixed 'pigz' binary, no shell
import tarfile
import threading

logger = logging.getLogger(__name__)

#: Excluded from every campaign archive by default: the postprocessing hash-cache
#: (``.cache``) is derived/rebuildable and never belongs in a shared or downloaded
#: campaign.
DEFAULT_EXCLUDE = frozenset({".cache"})

#: Read size for the download generator.
_CHUNK = 1024 * 1024


def _make_filter(exclude):
    """Return a ``tarfile.add`` filter dropping any member under an *exclude* name.

    Excluding a *directory* prunes its whole subtree: ``tarfile.add`` does not
    recurse into a member whose filter returns ``None``. That is how internal
    staging like ``_postproc/`` is kept out of a downloaded campaign.
    """
    exclude = frozenset(exclude or ())
    if not exclude:
        return None

    def _filter(tarinfo):
        # tarinfo.name is the arcname (``<campaign>/<rel>``); drop the member if any
        # path component matches an excluded name.
        if exclude.intersection(tarinfo.name.split("/")):
            return None
        return tarinfo

    return _filter


def _add_campaign_tree(tar: tarfile.TarFile, campaign_root: str, exclude) -> None:
    """Add the whole campaign tree under ``<campaign-id>/`` into *tar*.

    Relies on the TarFile's ``dereference=False`` (the default) so ``job`` symlinks
    are stored as symlink members and not followed/recursed.
    """
    arcname = os.path.basename(os.path.normpath(campaign_root))
    tar.add(campaign_root, arcname=arcname, filter=_make_filter(exclude))


def make_campaign_tarball(campaign_root: str, archive_dir: str,
                          exclude=DEFAULT_EXCLUDE, name: "str | None" = None) -> str:
    """Write the campaign at *campaign_root* into *archive_dir*; return its path.

    *name* is the file name to write, defaulting to ``<campaign>.tar.gz``. The local
    lane passes the variant-carrying name a share uses
    (:func:`~robovast.execution.share_providers.naming.archive_name`) so its
    ``_archives/`` dir and a real share are readable by the same parser.

    Uses Python's built-in gzip (no ``pigz`` dependency) since this runs on the
    local host where ``pigz`` may be absent; the stream variants use ``pigz`` on the
    driver/service image where it is present.
    """
    campaign_root = os.path.normpath(str(campaign_root))
    arcname = os.path.basename(campaign_root)
    os.makedirs(archive_dir, exist_ok=True)
    out_path = os.path.join(archive_dir, name or f"{arcname}.tar.gz")
    with tarfile.open(out_path, "w:gz") as tar:
        _add_campaign_tree(tar, campaign_root, exclude)
    logger.info("Wrote campaign archive %s", out_path)
    return out_path


class _TarPipe:
    """A running ``tar | pigz`` pipe: a writer thread tars into ``pigz`` stdin, and
    ``stdout`` is a readable stream of the compressed archive.

    *add_members* is a callable ``(tarfile.TarFile) -> None`` that adds every member —
    from a local directory (upload-to-share) or streamed from the object store
    (download). Neither source ever materialises a compressed copy on disk.
    """

    def __init__(self, add_members):
        self._add_members = add_members
        self._error: list = []
        # nosec B603 B607 - fixed binary, no shell, trusted args
        self._pigz = subprocess.Popen(
            ["pigz", "-c"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self._writer = threading.Thread(
            target=self._write_tar, name="campaign-tar-writer", daemon=True)
        self._writer.start()

    @property
    def stdout(self):
        return self._pigz.stdout

    def _write_tar(self) -> None:
        try:
            with tarfile.open(fileobj=self._pigz.stdin, mode="w|") as tar:
                self._add_members(tar)
        except BaseException as exc:  # pylint: disable=broad-except
            self._error.append(exc)
        finally:
            try:
                self._pigz.stdin.close()
            except OSError:
                pass

    def close(self) -> None:
        """Join the writer, reap ``pigz``, and re-raise any producer/compressor error."""
        try:
            self._pigz.stdout.close()
        except OSError:
            pass
        self._writer.join()
        self._pigz.wait()
        if self._error:
            raise self._error[0]
        if self._pigz.returncode not in (0, None):
            raise RuntimeError(f"pigz exited with code {self._pigz.returncode}")


@contextlib.contextmanager
def tar_stream(add_members):
    """Context manager yielding a **readable** gzip stream produced by *add_members*.

    No archive is written to disk. The yielded object is a binary file-like
    (``pigz`` stdout); read it to completion inside the ``with`` block. On exit the
    writer thread is joined and any tar/pigz failure is re-raised.
    """
    pipe = _TarPipe(add_members)
    try:
        yield pipe.stdout
    finally:
        pipe.close()


def iter_tar(add_members, chunk_size: int = _CHUNK):
    """Generator yielding gzip-archive bytes produced by *add_members*.

    Owns the pipe lifecycle across the whole iteration — cleanup (and error
    propagation) happens when the generator is exhausted or closed, which is what a
    streaming HTTP response needs (the body is produced after the route returns).
    """
    pipe = _TarPipe(add_members)
    try:
        while True:
            chunk = pipe.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        pipe.close()


def campaign_tar_stream(campaign_root: str, exclude=DEFAULT_EXCLUDE):
    """CM yielding a readable gzip stream of the local directory *campaign_root*."""
    return tar_stream(lambda tar: _add_campaign_tree(tar, campaign_root, exclude))


def iter_campaign_tar(campaign_root: str, exclude=DEFAULT_EXCLUDE, chunk_size: int = _CHUNK):
    """Generator yielding gzip-archive bytes of the local directory *campaign_root*."""
    return iter_tar(
        lambda tar: _add_campaign_tree(tar, campaign_root, exclude), chunk_size)
