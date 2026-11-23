# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The chain operator: a stage derived from a finished stage's record, by stated rules.

Each test builds a small ``campaign.db`` with the store's own writer, so the operator reads
exactly what a finished campaign leaves, and a template ``.vast`` beside it.
"""

import json

import pytest
import yaml
from click.testing import CliRunner

from robovast.common.store import STORE_FILENAME, CampaignStore
from robovast.search.chain import ChainError, band_cells, derive, load_record, narrow, select_cell

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


def _template(tmp_path, space=None, extra_markers=('form', 'x')):
    doc = {
        'version': 7,
        'metadata': {'name': 'stage'},
        'search': {
            'strategy': 'boundary',
            'search_space': dict(SPACE if space is None else space),
            'variations': [{'ParameterVariationList': {
                'sim': [f'components.{d}' for d in extra_markers],
                'values': [[f'${d}' for d in extra_markers]]}}],
        },
    }
    path = tmp_path / 'stage.template.vast'
    path.write_text('# A template header that says which file plugins it loads.\n'
                    + yaml.safe_dump(doc, sort_keys=False))
    return path


# -- reading a record --------------------------------------------------------

def test_a_record_reads_its_cells_objective_and_level(tmp_path):
    rec = load_record(_record(tmp_path))
    assert rec.campaign == 'rec-1'
    assert rec.objective == 'margin' and rec.direction == 'minimize'
    assert rec.level() == 0.0
    assert [c.objective for c in rec.cells] == [-20.0, -3.0, 2.0, 1.0, 30.0]


def test_a_record_is_found_in_the_directory_a_download_writes(tmp_path):
    _record(tmp_path)
    assert load_record(tmp_path).campaign == 'rec-1'


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


def test_a_choice_or_a_boolean_becomes_the_values_held():
    assert narrow(SPACE['form'], ['c', 'a', 'a'], 0.1) == {'type': 'choice',
                                                            'values': ['a', 'c']}
    assert narrow(SPACE['flag'], [True], 0.1) == {'type': 'choice', 'values': [True]}


# -- deriving ----------------------------------------------------------------

def _load(path):
    return yaml.safe_load(path.read_text())


def test_space_writes_the_band_box_with_its_provenance(tmp_path):
    out = tmp_path / 'stage.vast'
    derive(_record(tmp_path), _template(tmp_path), out, space=['x', 'form', 'flag'],
           band=5.0)
    doc = _load(out)
    space = doc['search']['search_space']
    assert space['x'] == {'type': 'float', 'low': 3.0, 'high': 7.0}
    assert space['form'] == {'type': 'choice', 'values': ['a', 'b', 'c']}
    assert space['flag'] == {'type': 'choice', 'values': [False, True]}
    assert space['n'] == SPACE['n']                       # not named, not touched
    (entry,) = doc['metadata']['derived_from']
    assert entry['campaign'] == 'rec-1' and entry['cells'] == 3
    assert entry['level'] == 0.0 and entry['band'] == 5.0
    text = out.read_text()
    assert text.startswith('# Derived from campaign rec-1')
    assert '# A template header that says which file plugins it loads.' in text


def test_fix_injects_the_selected_cell_and_keeps_the_template_s_own_dimensions(tmp_path):
    template = _template(tmp_path, space={'speed': {'type': 'float', 'low': 0, 'high': 2}})
    out = tmp_path / 'stage.vast'
    derive(_record(tmp_path), template, out, fix=['form', 'x'], select='nearest-level',
           feasible='success')
    doc = _load(out)
    assert doc['search']['search_space'] == {'speed': {'type': 'float', 'low': 0, 'high': 2}}
    assert doc['search']['variations'][0]['ParameterVariationList']['values'] == [['a', 5.0]]
    (entry,) = doc['metadata']['derived_from']
    assert entry['fix'] == {'form': 'a', 'x': 5.0}
    assert entry['select'] == 'nearest-level' and entry['feasible'] == 'success'


def test_provenance_accumulates_stage_by_stage(tmp_path):
    first = tmp_path / 'one.vast'
    derive(_record(tmp_path), _template(tmp_path), first, space=['x'], band=5.0)
    second = tmp_path / 'two.vast'
    derive(_record(tmp_path / 'r2', name='rec-2'), first, second, space=['n'], band=5.0)
    assert [e['campaign'] for e in _load(second)['metadata']['derived_from']] == ['rec-1',
                                                                                  'rec-2']


def test_a_batch_template_takes_fixed_values_and_refuses_narrowing(tmp_path):
    batch = tmp_path / 'batch.vast'
    batch.write_text(yaml.safe_dump({'version': 7, 'configuration': [
        {'name': 'decide', 'variations': [{'Layout': {'form': '$form', 'config': 'a'}},
                                          {'Layout': {'form': '$form', 'config': 'b'}}]}]}))
    out = tmp_path / 'out.vast'
    record = _record(tmp_path)
    derive(record, batch, out, fix=['form'], select='worst')
    variations = _load(out)['configuration'][0]['variations']
    assert [v['Layout']['form'] for v in variations] == ['a', 'a']
    with pytest.raises(ChainError, match='no search block'):
        derive(record, batch, out, space=['x'], band=5.0)


@pytest.mark.parametrize('kwargs, match', [
    ({}, 'nothing to derive'),
    ({'space': ['x']}, '--band'),
    ({'space': ['x'], 'band': 0.5}, 'fewer than the 3'),
    ({'space': ['x'], 'fix': ['x'], 'band': 5.0}, 'both narrowed and fixed'),
    ({'fix': ['n']}, r'no \$n marker'),
    ({'space': ['y'], 'band': 5.0}, "did not search 'y'"),
])
def test_an_argument_that_would_change_nothing_or_cannot_be_honoured_is_refused(
        tmp_path, kwargs, match):
    with pytest.raises(ChainError, match=match):
        derive(_record(tmp_path), _template(tmp_path), tmp_path / 'out.vast', **kwargs)


def test_space_needs_a_level_when_the_record_states_none(tmp_path):
    db = _record(tmp_path)
    import sqlite3
    con = sqlite3.connect(db)
    cfg = json.loads(con.execute('SELECT config_json FROM campaign').fetchone()[0])
    cfg['search']['strategy_parameters'] = {}
    con.execute('UPDATE campaign SET config_json = ?', (json.dumps(cfg),))
    con.commit()
    con.close()
    with pytest.raises(ChainError, match='needs a level'):
        derive(db, _template(tmp_path), tmp_path / 'out.vast', space=['x'], band=5.0)
    derive(db, _template(tmp_path), tmp_path / 'out.vast', space=['x'], band=5.0, level=0.0)


# -- the command -------------------------------------------------------------

def test_the_command_writes_the_stage_and_says_what_it_did(tmp_path):
    from robovast.configuration.configuration_utils.cli import configuration
    out = tmp_path / 'stage.vast'
    result = CliRunner().invoke(configuration, [
        'derive-stage', str(_record(tmp_path)), str(_template(tmp_path)), str(out),
        '--space', 'x', '--band', '5', '--fix', 'form', '--select', 'nearest-level',
        '--feasible', 'success'])
    assert result.exit_code == 0, result.output
    assert '3 cell(s) in the band narrowed x' in result.output
    assert 'form=a' in result.output
    assert out.is_file()


def test_the_command_exits_non_zero_on_a_refusal(tmp_path):
    from robovast.configuration.configuration_utils.cli import configuration
    result = CliRunner().invoke(configuration, [
        'derive-stage', str(_record(tmp_path)), str(_template(tmp_path)),
        str(tmp_path / 'o.vast'), '--space', 'x'])
    assert result.exit_code == 1
    assert '--band' in result.output
