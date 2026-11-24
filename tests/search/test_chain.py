# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The rules applied between two stages: a stage's cells read from the record, one selected,
a region narrowed.

Each test builds a small ``campaign.db`` with the store's own writer, so the rules read exactly
what a campaign records.
"""

import pytest

from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.search.chain import (ChainError, band_cells, load_record, narrow, region_box,
                                   select_cell)

SPACE = {
    'form': {'type': 'choice', 'values': ['a', 'b', 'c']},
    'x': {'type': 'float', 'low': 0.0, 'high': 10.0},
    'n': {'type': 'int', 'low': 1, 'high': 5},
    'flag': {'type': 'bool'},
}

# (form, x, n, flag, margin, success)
CELLS = [
    ('a', 1.0, 1, False, -20.0, 0),
    ('b', 4.0, 2, True, -3.0, 1),     # in the band, succeeds
    ('a', 5.0, 3, False, 2.0, 1),     # in the band, nearest the level among the successes
    ('c', 6.0, 4, True, 1.0, 0),      # in the band, nearest the level, fails
    ('b', 9.0, 5, False, 30.0, 1),
]


def _record(tmp_path, cells=None, *, direction='minimize', level=0.0, name='rec-1'):
    db = tmp_path / name / STORE_FILENAME
    db.parent.mkdir(parents=True)
    store = CampaignStore(db)
    config = {'search': {'strategy': 'boundary', 'search_space': SPACE,
                         'strategy_parameters': {'level': level},
                         'objectives': [{'name': 'margin', 'direction': direction}]}}
    cid = store.create_campaign(name, config)
    batch = store.open_batch(cid, 0, 'b0')
    for i, (form, x, n, flag, margin, ok) in enumerate(CELLS if cells is None else cells):
        store.record_unit(batch_id=batch, paramset_id=f'p{i}', config_name=f'c{i}',
                          params={'form': form, 'x': x, 'n': n, 'flag': flag},
                          objectives={'margin': margin}, measures={'success': ok},
                          status='ok', result_dir=f'c{i}', n_samples=4)
    store.close()
    return db


# -- reading a record --------------------------------------------------------

def test_a_record_reads_its_cells_objective_and_level(tmp_path):
    rec = load_record(_record(tmp_path))
    assert rec.campaign == 'rec-1'
    assert rec.objective == 'margin' and rec.direction == 'minimize'
    assert rec.level() == 0.0
    assert [c.objective for c in rec.cells] == [-20.0, -3.0, 2.0, 1.0, 30.0]


def test_a_record_is_found_in_the_campaign_s_directory(tmp_path):
    _record(tmp_path)
    assert load_record(tmp_path / 'rec-1').campaign == 'rec-1'


def test_a_path_without_a_record_is_refused(tmp_path):
    with pytest.raises(ChainError, match='no campaign.db'):
        load_record(tmp_path)


# -- selection rules ---------------------------------------------------------

def test_worst_is_the_extreme_the_direction_seeks(tmp_path):
    assert select_cell(load_record(_record(tmp_path)), 'worst').objective == -20.0
    rec = load_record(_record(tmp_path / 'm', direction='maximize'))
    assert select_cell(rec, 'worst').objective == 30.0


def test_nearest_level_and_feasibility(tmp_path):
    rec = load_record(_record(tmp_path))
    assert select_cell(rec, 'nearest-level', level=0.0).params['form'] == 'c'
    chosen = select_cell(rec, 'nearest-level', level=0.0, feasible='success')
    assert chosen.params == {'form': 'a', 'x': 5.0, 'n': 3, 'flag': False}


def test_feasible_min_is_the_lowest_succeeding_cell(tmp_path):
    rec = load_record(_record(tmp_path))
    assert select_cell(rec, 'feasible-min', feasible='success').objective == -3.0


def test_a_tie_goes_to_the_lower_objective_then_the_earlier_cell(tmp_path):
    cells = [('a', 1.0, 1, False, 2.0, 1), ('b', 2.0, 1, False, -2.0, 1),
             ('c', 3.0, 1, False, -2.0, 1)]
    rec = load_record(_record(tmp_path, cells))
    assert select_cell(rec, 'nearest-level', level=0.0).params['form'] == 'b'


def test_a_feasibility_measure_no_cell_carries_is_refused(tmp_path):
    with pytest.raises(ChainError, match="no measure 'reached'"):
        select_cell(load_record(_record(tmp_path)), 'worst', feasible='reached')


# -- narrowing ---------------------------------------------------------------

def test_the_band_is_in_the_objective_s_unit_around_a_non_zero_level(tmp_path):
    rec = load_record(_record(tmp_path))
    assert [c.objective for c in band_cells(rec, 0.0, 5.0)] == [-3.0, 2.0, 1.0]
    assert [c.objective for c in band_cells(rec, 25.0, 5.0)] == [30.0]


def test_a_float_is_padded_and_clipped_an_int_rounded_outward():
    assert narrow(SPACE['x'], [4.0, 6.0], 0.1) == {'type': 'float', 'low': 3.0, 'high': 7.0}
    assert narrow(SPACE['x'], [0.5, 6.0], 0.1)['low'] == 0.0
    assert narrow(SPACE['n'], [2, 3], 0.1) == {'type': 'int', 'low': 1, 'high': 4}


def test_a_stepped_int_keeps_its_lattice():
    """Edges rounded to the nearest integer would shift the lattice the codec snaps to: low 3
    with step 2 reaches 3 and 5, never the 4 the record held."""
    dim = {'type': 'int', 'low': 0, 'high': 10, 'step': 2}
    assert narrow(dim, [4], 0.1) == {'type': 'int', 'step': 2, 'low': 2, 'high': 6}
    assert narrow(dim, [10], 0.1)['high'] == 10


def test_a_log_dimension_is_padded_in_log_space():
    """A pad of the linear range would reach from 1 down to the floor of 0.01..100; in log
    space it is a tenth of the four decades on each side."""
    dim = {'type': 'float', 'low': 0.01, 'high': 100.0, 'log': True}
    out = narrow(dim, [1.0], 0.1)
    assert out['low'] == pytest.approx(10 ** -0.4, rel=1e-5)
    assert out['high'] == pytest.approx(10 ** 0.4, rel=1e-5)


def test_a_float_edge_is_rounded_outward_so_the_box_keeps_its_cells():
    dim = {'type': 'float', 'low': 0.0, 'high': 1.0}
    out = narrow(dim, [0.1234567, 0.7654321], 0.0)
    assert out['low'] <= 0.1234567 and out['high'] >= 0.7654321
    assert out == {'type': 'float', 'low': 0.123456, 'high': 0.765433}


def test_a_small_log_dimension_keeps_a_positive_range():
    dim = {'type': 'float', 'low': 1e-9, 'high': 1e-3, 'log': True}
    out = narrow(dim, [2e-8, 5e-8], 0.0)
    assert 0 < out['low'] <= 2e-8 and 5e-8 <= out['high'] < 1e-3


@pytest.mark.parametrize('dim, values', [
    ({'type': 'float', 'low': 0.0, 'high': 5.0}, [6.0, 9.0]),
    ({'type': 'int', 'low': 7, 'high': 9}, [1, 4]),
])
def test_cells_outside_the_declared_range_are_refused(dim, values):
    with pytest.raises(ChainError, match="outside the declared range"):
        narrow(dim, values, 0.1)


def test_a_dimension_the_schema_rejects_is_refused_by_name():
    with pytest.raises(ChainError, match='is no search dimension'):
        narrow({'type': 'float', 'low': 0.0, 'high': 1.0, 'log': True}, [0.5], 0.1)


def test_a_choice_or_a_boolean_becomes_the_values_held():
    assert narrow(SPACE['form'], ['c', 'a', 'a'], 0.1) == {'type': 'choice',
                                                            'values': ['a', 'c']}
    assert narrow(SPACE['flag'], [True], 0.1) == {'type': 'choice', 'values': [True]}


def test_nearest_level_needs_a_level(tmp_path):
    rec = load_record(_record(tmp_path))
    with pytest.raises(ChainError, match='nearest-level needs a level'):
        select_cell(rec, 'nearest-level')
    assert select_cell(rec, 'nearest-level', level=0.0).objective == 1.0


# -- a record of a search in stages ------------------------------------------

def _staged_record(tmp_path):
    """Two stages: 'shape' over x (level 0.0) and 'speed' over n (level 2.0), the second
    re-proposing one cell the first measured."""
    db = tmp_path / 'staged' / STORE_FILENAME
    db.parent.mkdir(parents=True)
    store = CampaignStore(db)
    config = {'search': {'search_space': SPACE,
                         'objectives': [{'name': 'margin', 'direction': 'minimize'}],
                         'stages': [
                             {'name': 'shape', 'over': ['x'], 'strategy': 'boundary',
                              'strategy_parameters': {'level': 0.0}},
                             {'name': 'speed', 'over': ['n'], 'strategy': 'boundary',
                              'strategy_parameters': {'level': 2.0},
                              'objectives': [{'name': 'hard', 'direction': 'maximize'}]}]}}
    cid = store.create_campaign('staged', config)
    shape, speed = store.declare_stages(cid, [{'name': 'shape', 'over': ['x']},
                                              {'name': 'speed', 'over': ['n']}])
    b0 = store.open_batch(cid, 0, '.', stage_id=shape)
    first = store.record_unit(batch_id=b0, paramset_id='p0', config_name='c0',
                              params={'x': 1.0, 'n': 1}, objectives={'margin': -1.0, 'hard': 5.0},
                              measures={}, status='evaluated', result_dir='c0')
    store.record_unit(batch_id=b0, paramset_id='p1', config_name='c1',
                      params={'x': 2.0, 'n': 1}, objectives={'margin': 3.0, 'hard': 1.0},
                      measures={}, status='evaluated', result_dir='c1')
    b1 = store.open_batch(cid, 1, '.', stage_id=speed)
    store.record_unit(batch_id=b1, paramset_id='p2', config_name='c2',
                      params={'x': 1.0, 'n': 4}, objectives={'margin': 0.0, 'hard': 2.0},
                      measures={}, status='evaluated', result_dir='c2')
    store.record_recall(b1, 'p0', {'x': 1.0, 'n': 1})
    store._conn.execute("UPDATE unit SET recalled_from = ? WHERE status = 'recalled'", (first,))
    store._conn.commit()
    store.close()
    return db


def test_a_record_of_several_stages_is_read_one_stage_at_a_time(tmp_path):
    db = _staged_record(tmp_path)
    with pytest.raises(ChainError, match=r"staged has stages \['shape', 'speed'\]; name the "
                                         "one to read"):
        load_record(db)
    with pytest.raises(ChainError, match="staged has no stage 'nope'"):
        load_record(db, stage='nope')

    shape = load_record(db, stage='shape')
    assert [c.params for c in shape.cells] == [{'x': 1.0, 'n': 1}, {'x': 2.0, 'n': 1}]
    assert (shape.objective, shape.level(), shape.label) == ('margin', 0.0, "stage 'shape'")

    speed = load_record(db, stage='speed')
    assert (speed.objective, speed.direction, speed.level()) == ('hard', 'maximize', 2.0)
    # The recalled cell is one of the stage's cells, read from the unit that measured it.
    assert sorted(c.objective for c in speed.cells) == [2.0, 5.0]


def test_a_stage_s_rules_name_the_stage(tmp_path):
    speed = load_record(_staged_record(tmp_path), stage='speed')
    with pytest.raises(ChainError, match="stage 'speed' has 1 cell"):
        region_box(speed, {'n': SPACE['n']}, level=2.0, band=0.5, pad=0.0, min_cells=2)
    box, cells = region_box(speed, {'n': SPACE['n']}, level=2.0, band=0.5, pad=0.0, min_cells=1)
    assert box == {'n': {'type': 'int', 'low': 4, 'high': 4}} and len(cells) == 1
    assert select_cell(speed, 'worst').objective == 5.0
