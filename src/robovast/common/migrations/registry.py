"""Every version surface in robovast, enumerated in one place.

The implementations deliberately stay where their schema lives -- ``store.py``'s
``_MIGRATIONS`` must sit beside ``_SCHEMA`` because a new column has to be mirrored into
both in the same order, and separating them would break the coupling
``test_fresh_and_migrated_schemas_match`` exists to protect. This module *imports* them so
there is one place that lists all four, and one place to assert their shared invariants.
"""


def surfaces() -> list:
    """``[{name, current, baseline, steps, where}]`` for every version surface.

    Imports are local: this module is a map, and a map should not drag the mapped code
    into every process that wants to read it.
    """
    from robovast.common import store  # pylint: disable=import-outside-toplevel
    from robovast.common.analysis import db  # pylint: disable=import-outside-toplevel

    from . import config as config_ladder  # pylint: disable=import-outside-toplevel

    return [
        {
            "name": "vast_config",
            "current": config_ladder.SUPPORTED_CONFIG_VERSION,
            "baseline": config_ladder.BASELINE_CONFIG_VERSION,
            "steps": len(config_ladder._MIGRATIONS),  # pylint: disable=protected-access
            "where": "robovast/common/migrations/config/",
        },
        {
            "name": "campaign_store",
            "current": store.SCHEMA_VERSION,
            "baseline": 0,
            "steps": len(store._MIGRATIONS),  # pylint: disable=protected-access
            "where": "robovast/common/store.py (beside _SCHEMA)",
        },
        {
            "name": "analysis_db",
            "current": db.DATA_DB_SCHEMA_VERSION,
            "baseline": 0,
            "steps": None,
            "where": "robovast/common/analysis/db.py",
        },
    ]
