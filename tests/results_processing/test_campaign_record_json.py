# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The campaign record's JSON columns, made castable on their way into the index."""

import json

from robovast.results_processing.dimension_ingest import _indexable


def _refuse_constant(token):
    raise AssertionError(f"{token} is not JSON and no strict parser accepts it")


def test_the_campaign_record_json_is_made_castable_on_its_way_into_the_index():
    """``campaign.db`` is written with Python's ``json``, whose ``Infinity`` token Postgres
    refuses on a ``jsonb`` cast; the mirror rewrites it and leaves everything else as is."""
    assert json.loads(_indexable("objectives_json", '{"length": Infinity, "gap": NaN}'),
                      parse_constant=_refuse_constant) == {"length": "inf", "gap": "nan"}
    assert _indexable("objectives_json", '{"length": 1.5}') == '{"length": 1.5}'
    assert _indexable("status", "Infinity") == "Infinity", "only the *_json columns"
