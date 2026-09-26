"""Archive layout 0 -> 1: the stamp.

Layout 0 is every archive written before :data:`~robovast.common.migrations.archive.ARCHIVE_STAMP`
existed, and its tree is exactly layout 1's: the stamp is the whole difference, and the ladder
writes it once the steps have run. So this step changes nothing in the tree.

**A step takes the extracted campaign directory and rewrites it in place.** It must not import
the models its records are read with now (``robovast.client.status``, ``robovast.common.config``,
``robovast.common.store``): a step that reads the *current* model changes meaning the next
time that model changes. It touches only paths inside the campaign directory it is given.
"""

from pathlib import Path


def migrate(campaign_dir: Path) -> None:
    """Carry *campaign_dir* from layout 0 to 1: nothing in the tree differs."""
    del campaign_dir
