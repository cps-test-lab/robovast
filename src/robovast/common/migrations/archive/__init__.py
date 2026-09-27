"""The campaign archive's layout ladder.

The ``.vast`` and ``campaign.db`` carry their own numbers; everything else a campaign tree
holds -- where its records sit and the formats of ``_execution/outcome.json``,
``launch.yaml``, ``execution.yaml`` and the other records -- is versioned by the **archive
layout**. A record whose format changes moves the layout, and a step rewrites an older tree.

Both streams in :mod:`robovast.execution.campaign_archive` write :data:`ARCHIVE_STAMP`, and
an import reads it before anything else in the tree
(:func:`robovast.service.ingest.ingest_campaign`). An archive with no stamp is layout
:data:`BASELINE_ARCHIVE_LAYOUT`.
"""

import json
from pathlib import Path

#: Campaign-relative path of the stamp.
ARCHIVE_STAMP = "_execution/archive.json"

#: The oldest layout, and the layout of an archive that carries no stamp.
BASELINE_ARCHIVE_LAYOUT = 1

#: The layout this robovast writes, and the one an import brings an older tree to.
ARCHIVE_LAYOUT = 1

#: ``_MIGRATIONS[i]`` is ``migrate(campaign_dir)`` from the module ``vN_to_vM.py`` beside this
#: one, carrying an extracted campaign tree from layout ``BASELINE_ARCHIVE_LAYOUT + i`` to
#: ``+ i + 1`` in place. **Append only; never edit an existing entry** -- an edit changes what
#: an archive already written would become.
_MIGRATIONS: list = []

assert len(_MIGRATIONS) == ARCHIVE_LAYOUT - BASELINE_ARCHIVE_LAYOUT


class ArchiveLayoutError(ValueError):
    """The stamp cannot be read as a layout, or a step could not carry the tree forward."""


class ArchiveTooNew(ArchiveLayoutError):
    """The archive was written by a robovast whose layout this one does not know."""


def archive_stamp(campaign_id: str) -> bytes:
    """The bytes of :data:`ARCHIVE_STAMP` for an archive of *campaign_id* written now.

    ``layout`` is the only field an importer acts on. The others are what the writing
    robovast was, for whoever has to decide what to do with an archive this one cannot read:
    its version as ``execution.yaml`` records it, and the numbers of the surfaces that carry their own (the ``.vast``
    ladder, the ``campaign.db`` schema, the data contract, the host-container protocol).
    """
    from robovast.common import store  # pylint: disable=import-outside-toplevel
    from robovast.common.execution import (  # pylint: disable=import-outside-toplevel
        COMPAT_VERSION, get_app_version)
    from robovast_decode import DATA_CONTRACT  # pylint: disable=import-outside-toplevel

    from .. import config  # pylint: disable=import-outside-toplevel

    return json.dumps({
        "layout": ARCHIVE_LAYOUT,
        "campaign_id": campaign_id,
        "robovast": get_app_version(),
        "config_version": config.SUPPORTED_CONFIG_VERSION,
        "store_schema": store.SCHEMA_VERSION,
        "data_contract": DATA_CONTRACT,
        "compat_version": COMPAT_VERSION,
    }, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def read_layout(campaign_dir) -> "tuple[int, dict]":
    """``(layout, stamp)`` of the campaign tree at *campaign_dir*.

    No stamp is :data:`BASELINE_ARCHIVE_LAYOUT` with an empty stamp. A stamp that cannot be
    read as a layout raises :class:`ArchiveLayoutError` rather than being guessed at.
    """
    path = Path(campaign_dir) / ARCHIVE_STAMP
    if not path.is_file():
        return BASELINE_ARCHIVE_LAYOUT, {}
    try:
        stamp = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ArchiveLayoutError(f"{ARCHIVE_STAMP} is not readable as JSON: {e}") from e
    layout = stamp.get("layout") if isinstance(stamp, dict) else None
    if (not isinstance(layout, int) or isinstance(layout, bool)
            or layout < BASELINE_ARCHIVE_LAYOUT):
        raise ArchiveLayoutError(
            f"{ARCHIVE_STAMP} states no layout this robovast can read (layout: {layout!r}); "
            f"the layouts it knows are {BASELINE_ARCHIVE_LAYOUT}..{ARCHIVE_LAYOUT}.")
    return layout, stamp


def upgrade_archive(campaign_dir) -> "tuple[int, list[str]]":
    """Bring the extracted tree at *campaign_dir* to :data:`ARCHIVE_LAYOUT`, in place.

    Returns ``(layout found, steps applied)``. When a step ran, the stamp is rewritten to the
    layout the tree is now at, keeping what the writer recorded and adding ``layout_from``,
    so the tree never claims a layout it is not in.

    Raises:
        ArchiveTooNew: the stamp names a layout above :data:`ARCHIVE_LAYOUT`.
        ArchiveLayoutError: the stamp is unreadable, or a step failed.
    """
    campaign_dir = Path(campaign_dir)
    found, stamp = read_layout(campaign_dir)
    if found > ARCHIVE_LAYOUT:
        raise ArchiveTooNew(
            f"archive layout {found} was written by a newer robovast "
            f"({stamp.get('robovast', 'version not recorded')}); this one reads layouts up "
            f"to {ARCHIVE_LAYOUT}. A layout cannot be migrated backwards -- upgrade robovast.")
    applied: list[str] = []
    for layout in range(found, ARCHIVE_LAYOUT):
        step = _MIGRATIONS[layout - BASELINE_ARCHIVE_LAYOUT]
        try:
            step(campaign_dir)
        except Exception as e:  # pylint: disable=broad-except
            raise ArchiveLayoutError(
                f"the archive layout step {layout}_to_{layout + 1} failed: {e}") from e
        applied.append(f"{layout}_to_{layout + 1}")
    if applied:
        (campaign_dir / ARCHIVE_STAMP).parent.mkdir(parents=True, exist_ok=True)
        (campaign_dir / ARCHIVE_STAMP).write_text(json.dumps(
            {**stamp, "layout": ARCHIVE_LAYOUT, "layout_from": found},
            indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return found, applied
