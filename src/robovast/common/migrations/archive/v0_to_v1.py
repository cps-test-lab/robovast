"""Archive layout 0 -> 1: the stamp.

Layout 0 is an archive without :data:`~robovast.common.migrations.archive.ARCHIVE_STAMP`. Its
tree is layout 1's, and the ladder writes the stamp, so this step changes nothing.
"""

from pathlib import Path


def migrate(campaign_dir: Path) -> None:
    """Carry *campaign_dir* from layout 0 to 1: nothing in the tree differs."""
    del campaign_dir
