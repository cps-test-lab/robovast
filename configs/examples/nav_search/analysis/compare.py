#!/usr/bin/env python3
"""Read several nav_search campaigns against each other.

The findings this directory is FOR are pairings, and none of them is visible from a single
campaign: an analysis notebook is scoped to one campaign, so nothing else here can compute
them. This is that missing half -- `random` against `halton`, `tpe` against `cmaes`, `tpe`
against `adaptive_reps`, and the grid against all of them.

It reads each campaign's own store (`campaign.db`), which carries both the scored cells and
the `.vast` that produced them. Nothing is passed in about what a campaign *is*: the
strategy, its budget and its seed are read from the record, so a comparison cannot be
labelled with a strategy the campaign did not run.

Usage:
    vast files get /results/<campaign_id>/campaign.db <dir>/<campaign_id>.db   # per campaign
    python analysis/compare.py <dir>/*.db

A campaign directory works too, if you have one downloaded whole.
"""
import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path


def _store(path: Path) -> Path:
    """Accept either a campaign.db or the campaign directory holding one."""
    if path.is_dir():
        candidate = path / 'campaign.db'
        if not candidate.is_file():
            raise SystemExit(f'{path} is a directory with no campaign.db in it')
        return candidate
    return path


class Campaign:
    """One campaign's record: what it ran, and what it scored."""

    def __init__(self, path: Path):
        self.path = _store(path)
        conn = sqlite3.connect(f'file:{self.path}?mode=ro', uri=True)
        try:
            row = conn.execute(
                'SELECT name, mode, config_json FROM campaign ORDER BY id LIMIT 1').fetchone()
            if row is None:
                raise SystemExit(f'{self.path} has no campaign row')
            self.campaign_id, self.mode, config = row[0], row[1], json.loads(row[2])
            self.units = self._units(conn)
            self.cell_failed = self._cell_failed(conn)
        finally:
            conn.close()

        self.name = (config.get('metadata') or {}).get('name') or self.campaign_id
        search = config.get('search') or {}
        self.strategy = search.get('strategy')
        self.strategy_parameters = search.get('strategy_parameters') or {}
        self.seed = search.get('seed')
        self.repetitions = search.get('repetitions')
        # Budget as the campaign DECLARED it. Two campaigns are comparable only if these
        # agree -- see require_same_budget, which is why it is kept rather than summarised.
        self.budget = {b.get('type'): b.get('value') for b in (search.get('budget') or [])}
        self.runs_declared = (config.get('execution') or {}).get('runs')

    def _units(self, conn):
        """The scored cells: parameters, objectives and measures, one row per cell."""
        rows = conn.execute(
            'SELECT u.id, u.params_json, u.objectives_json, u.measures_json, u.n_samples,'
            '       u.status, b.idx'
            '  FROM unit u LEFT JOIN batch b ON b.id = u.batch_id'
            ' ORDER BY b.idx, u.id').fetchall()
        units = []
        for uid, params, objectives, measures, samples, status, batch in rows:
            unit = {'unit_id': uid, 'n_samples': samples or 0, 'status': status,
                    'batch': batch}
            unit.update(json.loads(params or '{}'))
            unit.update(json.loads(objectives or '{}'))
            unit.update({f'm_{k}': v for k, v in json.loads(measures or '{}').items()})
            units.append(unit)
        return units

    def _cell_failed(self, conn):
        """Per cell, whether ANY of its repetitions failed.

        The batch-mode grid scores no `robustness` -- it declares no extractor, so its cells
        carry no objective. This is the one verdict both kinds of campaign can state, and it
        is the same question `aggregate: worst` asks of a search: *can this cell fail?*
        """
        rows = conn.execute(
            'SELECT unit_id, MIN(passed), COUNT(passed) FROM run GROUP BY unit_id').fetchall()
        # A cell none of whose runs recorded an outcome is left OUT rather than counted as
        # passing: it is unknown, and folding it in as a pass would deflate every fraction
        # below by exactly the runs that failed to report.
        return {uid: worst != 1 for uid, worst, recorded in rows if recorded}

    @property
    def scored(self):
        return [u for u in self.units if u['status'] == 'evaluated']

    @property
    def runs_spent(self):
        return sum(u['n_samples'] for u in self.units)

    @property
    def worst(self):
        values = [u['robustness'] for u in self.scored if u.get('robustness') is not None]
        return min(values) if values else None

    def failing_cell_fraction(self):
        """(failing cells, cells) -- from `robustness` where there is one, else run outcomes.

        Both spellings answer the same question, so the grid and a search can be put in one
        column; which was used is reported, because they are not the same measurement.
        """
        scored = self.scored
        if scored and any(u.get('robustness') is not None for u in scored):
            failed = sum(1 for u in scored if (u.get('robustness') or 0) < 0)
            return failed, len(scored), 'robustness < 0'
        verdicts = [self.cell_failed[u['unit_id']] for u in self.units
                    if u['unit_id'] in self.cell_failed]
        return sum(verdicts), len(verdicts), 'any repetition failed'


def wilson(k, n, z=1.96):
    """95% interval for a proportion. A fraction reported without one is not an estimate."""
    if not n:
        return (float('nan'), float('nan'))
    phat = k / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (centre - half, centre + half)


def runs_to_depth(campaign, target):
    """Runs spent before this campaign had reached *target* robustness.

    Counted A BATCH AT A TIME, which is what a search actually costs. A batch is proposed as
    a set, its cells run in parallel and their scores come back together -- so a campaign that
    found the depth in the third cell of a batch still paid for all of it, and nobody could
    have stopped earlier. Counting cell by cell answers a question no operator can act on and
    flatters whichever sampler happened to order a good cell first within its batch.

    `None` when the depth was never reached, which is itself the answer and not a large number.
    """
    spent, depth = 0, None
    for _, cells in _by_batch(campaign):
        spent += sum(u['n_samples'] for u in cells)
        values = [u['robustness'] for u in cells if u.get('robustness') is not None]
        if not values:
            continue
        depth = min(values) if depth is None else min(depth, min(values))
        if depth <= target:
            return spent
    return None


def _by_batch(campaign):
    """The scored cells grouped by the round that proposed them, in order."""
    rounds = {}
    for unit in campaign.scored:
        rounds.setdefault(unit.get('batch'), []).append(unit)
    return sorted(rounds.items(), key=lambda kv: (kv[0] is None, kv[0]))


def require_same_budget(a, b):
    """A pairing is only a contest if both were given the same simulator.

    Stated in code rather than left to whoever runs this: equal *batches* are not equal
    simulator once repetitions stop being constant, so the check is on the declared budget.
    Reported as a warning rather than refused -- an unequal comparison someone means to make
    is still worth printing, as long as it cannot be mistaken for a fair one.
    """
    if a.budget != b.budget:
        print(f'  !! BUDGETS DIFFER: {a.name} {a.budget} vs {b.name} {b.budget}')
        print('     These two are not a contest. Read the numbers below as two separate')
        print('     campaigns, not as one strategy beating the other.')
        return False
    return True


def pick(campaigns, *names):
    """The campaign whose declared metadata name matches, or None."""
    for campaign in campaigns:
        if campaign.name in names:
            return campaign
    return None


def roster(campaigns):
    print('=' * 78)
    print('THE CAMPAIGNS')
    print('=' * 78)
    header = f'{"name":<26}{"strategy":<16}{"budget":<16}{"cells":>7}{"runs":>7}{"worst":>9}'
    print(header)
    print('-' * len(header))
    for campaign in sorted(campaigns, key=lambda c: c.name):
        sampler = campaign.strategy_parameters.get('sampler')
        strategy = f'{campaign.strategy or "batch"}'
        if sampler:
            strategy = f'{strategy}/{sampler}'
        budget = ', '.join(f'{k}={v}' for k, v in campaign.budget.items()) or '-'
        worst = f'{campaign.worst:.3f}' if campaign.worst is not None else '-'
        print(f'{campaign.name:<26}{strategy[:15]:<16}{budget[:15]:<16}'
              f'{len(campaign.scored):>7}{campaign.runs_spent:>7}{worst:>9}')
    print()


def coverage_pair(campaigns):
    a = pick(campaigns, 'nav_search_random')
    b = pick(campaigns, 'nav_search_halton')
    if not (a and b):
        return
    print('=' * 78)
    print('COVERAGE: random vs halton -- the same fraction, drawn two ways')
    print('=' * 78)
    require_same_budget(a, b)
    for campaign in (a, b):
        k, n, how = campaign.failing_cell_fraction()
        low, high = wilson(k, n)
        print(f'  {campaign.name:<26} {k}/{n} cells fail  = {k / n:6.1%}'
              f'   95% CI [{low:.1%}, {high:.1%}]   ({how})')
    ka, na, _ = a.failing_cell_fraction()
    kb, nb, _ = b.failing_cell_fraction()
    lowa, higha = wilson(ka, na)
    lowb, highb = wilson(kb, nb)
    overlap = lowa <= highb and lowb <= higha
    print()
    print(f'  intervals overlap : {overlap}')
    print('  Two point estimates of ONE quantity. Overlapping intervals are the expected')
    print('  result: the question is not which is higher but whether the evenly-covering')
    print('  sample estimates it more tightly -- and an interval WIDTH is a property of an')
    print('  estimator across repeats, so answering that needs each run over several seeds.')
    print(f'  Both of these ran at seed {a.seed} and {b.seed}: one draw each, not a spread.')
    print()


def sampler_pair(campaigns):
    a = pick(campaigns, 'nav_search_tpe')
    b = pick(campaigns, 'nav_search_cmaes')
    if not (a and b):
        return
    print('=' * 78)
    print('SAMPLERS: tpe vs cmaes -- convergence at an equal budget')
    print('=' * 78)
    require_same_budget(a, b)
    worsts = [c.worst for c in (a, b) if c.worst is not None]
    if not worsts:
        print('  neither campaign scored a cell')
        print()
        return
    # Judged at the depth BOTH reached, so the comparison is "who got here first" rather
    # than "who went deeper" -- the second is a different question and a single draw of it.
    shared = max(worsts)
    print(f'  deepest crossing found : {a.name} {a.worst}   {b.name} {b.worst}')
    print(f'  compared at the depth both reached: {shared:.3f}')
    for campaign in (a, b):
        n = runs_to_depth(campaign, shared)
        spent = campaign.runs_spent
        print(f'  {campaign.name:<26} {n if n else "never"} run(s) to reach it'
              f'   ({spent} runs spent in total)')
    print()
    print('  Fewer RUNS to the same depth is the whole claim, counted a batch at a time')
    print('  because that is the smallest amount of simulator anyone could have bought.')
    print('  The final number alone is one draw of a stochastic search and settles nothing.')
    print()


def budget_pair(campaigns):
    a = pick(campaigns, 'nav_search_tpe')
    b = pick(campaigns, 'nav_search_adaptive_reps')
    if not (a and b):
        return
    print('=' * 78)
    print('REPETITIONS: tpe vs adaptive_reps -- what the same budget buys')
    print('=' * 78)
    require_same_budget(a, b)
    print()
    for campaign in (a, b):
        samples = [u['n_samples'] for u in campaign.scored if u['n_samples']]
        reps = f'{sum(samples) / len(samples):.2f}' if samples else '-'
        worst = f'{campaign.worst:.3f}' if campaign.worst is not None else '-'
        print(f'  {campaign.name:<26} worst {worst:>8}   {campaign.runs_spent:>4} runs'
              f'   {len(campaign.scored):>3} cells   mean {reps} reps/cell')
    print()
    if a.worst is not None and b.worst is not None:
        # At an EQUAL budget the claim is not "spent less" -- neither did -- but whether the
        # policy turned the same spend into a better answer. Measured, it reallocates rather
        # than saves: repetitions go where a cell's outcome is still in doubt, and on a
        # bimodal outcome that is worth more than the runs it would have saved.
        cells = f'{len(b.scored)} vs {len(a.scored)}'
        print(f'  cells evaluated  : {cells}')
        print(f'  found it as deep : {b.worst <= a.worst}   ({b.worst:.3f} vs {a.worst:.3f})')
        print('  At one budget, more cells AND an equal-or-deeper worst case is the claim.')
        print('  Fewer cells with a deeper worst is the policy paying for accuracy with')
        print('  coverage, which for a search that has to rank cells is the better trade.')
    print()


def grid_reference(campaigns):
    grid = pick(campaigns, 'nav_grid')
    searches = [c for c in campaigns if c is not grid and c.scored]
    if not (grid and searches):
        return
    print('=' * 78)
    print('THE REFERENCE: the grid against everything')
    print('=' * 78)
    k, n, how = grid.failing_cell_fraction()
    print(f'  {grid.name}: {n} cells covered exhaustively, {grid.runs_spent} runs')
    print(f'    {k}/{n} cells can fail = {k / n:.1%}   ({how})')
    print()
    print('  What each search spent to say something about the same space:')
    for campaign in sorted(searches, key=lambda c: c.runs_spent):
        ks, ns, hows = campaign.failing_cell_fraction()
        # As a MULTIPLE of the grid's runs, not as a percentage saved. A search that spent
        # more than the exhaustive grid is the interesting outcome here, and a signed
        # percentage renders that as "-40%", which reads as the opposite of what it means.
        ratio = campaign.runs_spent / grid.runs_spent if grid.runs_spent else float('nan')
        worst = f'{campaign.worst:.3f}' if campaign.worst is not None else '-'
        print(f'    {campaign.name:<26} {campaign.runs_spent:>4} runs'
              f'  ({ratio:.2f}x the grid)   {ks}/{ns} cells fail   worst {worst:>8}')
    print()
    print('  The grid is the only campaign that can say what the others MISSED, but only')
    print('  about the cells it holds: it covers the space on a lattice, so a failure')
    print('  region narrower than its spacing is invisible to it too. A search finding a')
    print('  crossing worse than the grid\'s worst is that region, found.')
    print()
    # The two failing-cell columns above are NOT one quantity measured twice, and printing
    # them adjacent invites exactly that reading. Said here rather than left to the reader,
    # because the grid's figure is the larger one and "the searches missed things" is the
    # wrong conclusion it leads to.
    print('  READ THE TWO FAILING-CELL COLUMNS SEPARATELY. The grid\'s fraction is not an')
    print('  estimate of how much of the space fails, for two reasons that both inflate it:')
    print('    - a lattice is not a uniform sample. This grid puts a quarter of its cells at')
    print('      the narrowest doorway, where everything fails; uniform sampling puts a')
    print('      twelfth of them there.')
    print('    - more repetitions per cell detect a bimodal cell\'s bad mode more often, and')
    print('      the grid spends more per cell than the coverage campaigns do.')
    print('  Only a campaign that sampled the space uniformly estimates that fraction.')
    print()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('campaigns', nargs='+', type=Path,
                        help='campaign.db files, or campaign directories holding one')
    args = parser.parse_args(argv)

    campaigns = [Campaign(p) for p in args.campaigns]
    roster(campaigns)
    coverage_pair(campaigns)
    sampler_pair(campaigns)
    budget_pair(campaigns)
    grid_reference(campaigns)

    named = {c.name for c in campaigns}
    wanted = {'nav_search_random', 'nav_search_halton', 'nav_search_tpe',
              'nav_search_cmaes', 'nav_search_adaptive_reps', 'nav_grid'}
    missing = sorted(wanted - named)
    if missing:
        # Named, not silently skipped: a pairing that was never printed because its campaign
        # was not passed in looks exactly like one that had nothing to say.
        print(f'not compared, because these were not given: {", ".join(missing)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
