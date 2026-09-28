"""Config version 6 -> 7: the top-level ``general:`` section is gone.

In v6 ``general:`` accepted any mapping and robovast handed it, unread, to every variation
plugin as its ``general_parameters`` constructor argument. No part of robovast read a value
from it, so it configured nothing, and v7 refuses the key. A value a plugin needs belongs in
that plugin's own parameters, where its ``CONFIG_CLASS`` validates it.

The step drops the section. That changes nothing robovast composes or runs; a plugin that
read ``general_parameters`` no longer constructs under v7 whatever the file says, and moves
the value into its own parameters.

**Pure ``dict`` -> ``dict``.** Nothing here may import :mod:`robovast.common.config`;
``test_migration_purity`` enforces it. Deep-copy the input and mutate the copy -- never
``dict(raw)``, which drops a ruamel ``CommentedMap``'s comments.
"""

import copy


def migrate(raw: dict) -> dict:
    """Return *raw* restructured as a version 7 config. Does not mutate the input."""
    out = copy.deepcopy(raw)
    out.pop("general", None)
    out["version"] = 7
    return out
