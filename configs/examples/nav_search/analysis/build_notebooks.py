#!/usr/bin/env python3
"""Generate one campaign notebook per search campaign.

Generated rather than hand-edited for the same reason ``basic_nav`` generates its own: a
notebook is JSON with source split across a list of lines, so reviewing a one-character
change to a plot means reading a diff of escaped strings, and every edit risks producing a
file Jupyter will not open. The Python here is the reviewable form; the ``.ipynb`` beside
it is the artifact.

Regenerate with::

    python3 configs/examples/nav_search/analysis/build_notebooks.py

**Four cells, one figure.** Deliberately. The quadrotor example's QD notebook is 282 lines
rendering six pairwise projections; it is thorough and nobody reads it. Each notebook here
answers two questions -- *why this strategy* and *what did it find* -- and shows one picture
that makes the answer visible. The headline numbers are computed from the campaign, never
written into the text, so a notebook cannot claim a finding the data does not support.

The contract with the renderer (``robovast.results_processing.notebook_render``): each
notebook must contain a literal ``DATA_DIR = ...`` line, which is replaced with the absolute
path of the node being viewed before the cells are executed.
"""

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

# --- shared cells -----------------------------------------------------------

LOAD = '''\
# DATA_DIR is replaced by the service with the node being viewed. The assignment must stay
# a plain literal for that substitution to work.
DATA_DIR = ''

import json, os, sqlite3
import pandas as pd
import matplotlib.pyplot as plt

def load_units(data_dir):
    """One row per evaluated cell: its parameters, objectives and measures.

    Read from campaign.db rather than data.db because that is where a SEARCH records what
    it scored -- data.db holds per-run tables, and a search's unit of analysis is the cell.
    """
    db = os.path.join(data_dir, 'campaign.db')
    if not os.path.exists(db):
        return pd.DataFrame()
    with sqlite3.connect(db) as conn:
        units = pd.read_sql_query(
            "SELECT u.paramset_id, u.config_name, u.params_json, u.objectives_json,"
            "       u.measures_json, u.n_samples, u.status, b.idx AS batch"
            "  FROM unit u LEFT JOIN batch b ON b.id = u.batch_id"
            " ORDER BY b.idx, u.id", conn)
    if units.empty:
        return units
    for col, prefix in (('params_json', ''), ('objectives_json', ''), ('measures_json', 'm_')):
        expanded = units[col].apply(lambda s: json.loads(s) if s else {}).apply(pd.Series)
        expanded.columns = [f'{prefix}{c}' for c in expanded.columns]
        units = pd.concat([units.drop(columns=[col]), expanded], axis=1)
    return units

units = load_units(DATA_DIR)
scored = units[units['status'] == 'evaluated'] if 'status' in units else units
print(f"{len(units)} cell(s) recorded, {len(scored)} scored")
'''

NOTE = '''\
if scored.empty:
    print("No scored cells yet. A search records a cell once its batch has been evaluated;"
          "\\nif this campaign failed early, its controller log says why.")
'''


# Cell ids are required from nbformat 4.5 (`nbformat_minor: 5`) and their absence is
# already a deprecation warning on the way to a hard error. Named for the cell's ROLE
# rather than numbered, so regenerating a notebook whose cells shift produces no
# spurious diff and a traceback names the cell a reader can find.
def markdown(text, cell_id):
    return {"cell_type": "markdown", "id": cell_id, "metadata": {},
            "source": [line + "\n" for line in text.rstrip().split("\n")]}


def code(text, cell_id):
    return {"cell_type": "code", "id": cell_id, "execution_count": None, "metadata": {},
            "outputs": [], "source": [line + "\n" for line in text.rstrip().split("\n")]}


def notebook(why, figure, headline):
    """The fixed four-cell shape, so all eight read the same way."""
    return {
        "cells": [markdown(why, "why"), code(LOAD + "\n" + NOTE, "load"),
                  code(figure, "figure"), code(headline, "headline")],
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python",
                                    "name": "python3"},
                     "language_info": {"name": "python"}},
        "nbformat": 4, "nbformat_minor": 5,
    }


# --- the one figure, per campaign -------------------------------------------

SCATTER = '''\
# Where it sampled, and what it found there. Two axes because the space has two -- no
# projection, no facets, nothing to reconstruct in your head.
if not scored.empty:
    fig, ax = plt.subplots(figsize=(6, 4.5))
    pts = ax.scatter(scored['gap_width'], scored['walker_dwell'],
                     c=scored['robustness'], cmap='RdYlGn', vmin=-1, vmax=1,
                     s=90, edgecolor='black', linewidth=0.4)
    fig.colorbar(pts, ax=ax, label='robustness  (< 0 failed)')
    ax.set_xlabel('doorway width [m]')
    ax.set_ylabel("walker dwell [s]  (when it is in the way)")
    ax.set_title('%s: %d cells' % (TITLE, len(scored)))
    plt.tight_layout(); plt.show()
'''

CONVERGENCE = '''\
# Best-so-far against evaluations. The question is not what it found but how fast, so the
# x axis is spend and the line is the only thing that matters.
if not scored.empty:
    order = scored.reset_index(drop=True)
    best_so_far = order['robustness'].cummin()
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(range(1, len(order) + 1), best_so_far, drawstyle='steps-post', linewidth=2)
    ax.scatter(range(1, len(order) + 1), order['robustness'], s=18, alpha=0.45,
               label='each cell')
    ax.axhline(0, color='grey', linestyle='--', linewidth=1, label='failure boundary')
    ax.set_xlabel('cells evaluated'); ax.set_ylabel('robustness')
    ax.set_title('%s: worst found so far' % TITLE)
    ax.legend(); plt.tight_layout(); plt.show()
'''

ARCHIVE = '''\
# The archive as it is keyed: which KIND of trouble, against how close it came. A filled
# square is a kind that actually happens; an empty one is a kind that does not.
if not scored.empty and 'm_failure_mode' in scored:
    modes = ['none', 'collision', 'timeout', 'goal_miss']
    fig, ax = plt.subplots(figsize=(6.5, 4))
    for i, mode in enumerate(modes):
        rows = scored[scored['m_failure_mode'] == mode]
        ax.scatter(rows['m_min_clearance'], [i] * len(rows),
                   s=110, alpha=0.75, edgecolor='black', linewidth=0.4)
    ax.set_yticks(range(len(modes))); ax.set_yticklabels(modes)
    ax.set_xlabel('minimum clearance [m]'); ax.set_ylabel('failure mode')
    ax.axvline(0, color='crimson', linestyle='--', linewidth=1, label='contact')
    ax.set_title('%s: %d distinct kind(s) observed'
                 % (TITLE, scored['m_failure_mode'].nunique()))
    ax.legend(); plt.tight_layout(); plt.show()
'''

FRONT = '''\
# Clearance against time. The highlighted points are the trade-offs actually available:
# nothing else beats them on both at once.
if not scored.empty and 'm_time_to_goal' in scored:
    x, y = scored['m_min_clearance'], scored['m_time_to_goal']
    on_front = [not ((x > xi) & (y < yi)).any() for xi, yi in zip(x, y)]
    fig, ax = plt.subplots(figsize=(6, 4.5))
    ax.scatter(x, y, s=60, alpha=0.4, label='evaluated')
    ax.scatter(x[on_front], y[on_front], s=120, edgecolor='black', linewidth=0.8,
               color='tab:orange', label='non-dominated')
    ax.set_xlabel('minimum clearance [m]  (more is safer)')
    ax.set_ylabel('time to goal [s]  (less is better)')
    ax.set_title('%s: what safety costs' % TITLE)
    ax.legend(); plt.tight_layout(); plt.show()
'''

REPS = '''\
# How many repetitions each cell was given. A flat line is a campaign that spent the same
# everywhere; a spread one is a campaign that spent where the outcome was in doubt.
if not scored.empty:
    fig, ax = plt.subplots(figsize=(6.5, 4))
    ax.bar(range(len(scored)), scored['n_samples'], color='tab:blue', alpha=0.8)
    ax.set_xlabel('cell (in evaluation order)'); ax.set_ylabel('repetitions run')
    ax.set_title('%s: %d runs over %d cells'
                 % (TITLE, int(scored['n_samples'].sum()), len(scored)))
    plt.tight_layout(); plt.show()
'''

# --- headline numbers --------------------------------------------------------
#
# BASE is what every campaign can say about itself. ANSWER is what THIS campaign was run
# to find out, and there is one per strategy, because a notebook that prints the same
# numbers as its neighbour cannot answer a question its neighbour was not asked. Each is
# computed from the campaign and never written into the prose above it.

BASE = """\
if not scored.empty:
    failed = (scored['robustness'] < 0).sum()
    print(f"cells scored          : {len(scored)}")
    print(f"runs spent            : {int(scored['n_samples'].sum())}")
    print(f"cells that failed     : {failed}  ({failed / len(scored):.0%})")
    print(f"worst robustness      : {scored['robustness'].min():.3f}")
    print()
"""

# random / halton: a fraction of a space, so the answer is that fraction and how well it is
# pinned down. Wilson rather than Wald -- at 60 cells and a fraction near 0.7 the normal
# approximation's interval runs past what a proportion can be.
COVERAGE_ANSWER = """\
    import math
    n = len(scored); k = int(failed); phat = k / n
    z = 1.96
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    print("What this campaign is FOR -- the fraction of the space that fails:")
    print(f"  {phat:.1%} of cells  (95% Wilson interval {centre - half:.1%} .. {centre + half:.1%})")
    print(f"  interval width      : {2 * half:.1%} over {n} cells")
    print("  A DIFFERENT strategy's fraction is read against this one. To compare the")
    print("  WIDTH of two intervals, run each over several seed: values -- a spread is a")
    print("  property of an estimator across repeats, not something one campaign reports.")
"""

# tpe / cmaes: adversarial, so the answer is not what was found but how fast. Measured
# against the campaign's OWN best, so no external threshold has to be agreed on.
CONVERGENCE_ANSWER = """\
    order = scored.reset_index(drop=True)
    best = order['robustness'].cummin()
    final = best.iloc[-1]
    print("What this campaign is FOR -- how FEW evaluations found it:")
    print(f"  best crossing found : {final:.3f}")
    for frac in (0.5, 0.9, 1.0):
        target = final * frac
        hit = (best <= target).idxmax() + 1 if (best <= target).any() else None
        label = f"{frac:.0%} of that depth"
        print(f"  {label:<20}: {hit} evaluation(s)" if hit else f"  {label:<20}: not reached")
    print(f"  evaluations spent   : {len(order)}")
    print("  Read against the other sampler at the same runs: budget. Fewer evaluations to")
    print("  the same depth is the whole claim; the final number alone is not.")
"""

QD_ANSWER = """\
    modes = scored['m_failure_mode'].value_counts() if 'm_failure_mode' in scored else {}
    print("What this campaign is FOR -- how many KINDS of trouble exist:")
    print(f"  distinct failure modes found : {len(modes)}")
    for mode, count in modes.items():
        sub = scored[scored['m_failure_mode'] == mode]['robustness']
        print(f"    {mode:<12} {count:>3} cell(s), robustness {sub.min():.3f} .. {sub.max():.3f}")
    print("  An archive is judged on the SPREAD it fills, not on its best cell: two modes")
    print("  found once each beat one mode found forty times.")
"""

BOUNDARY_ANSWER = """\
    band = scored[scored['robustness'].abs() <= 0.05]
    print("What this campaign is FOR -- WHERE it starts failing:")
    print(f"  cells within +-0.05 of the boundary : {len(band)} of {len(scored)}")
    if not band.empty:
        for col, label in (('gap_width', 'doorway [m]'), ('walker_dwell', 'walker dwell [s]')):
            if col in band:
                print(f"    {label:<18} {band[col].min():.2f} .. {band[col].max():.2f}")
        print("  A boundary that spans a RANGE on both axes is a contour, not a threshold:")
        print("  neither factor alone decides the outcome.")
"""

REPS_ANSWER = """\
    reps = scored['n_samples']
    spent = int(reps.sum())
    flat = 3 * len(scored)
    print("What this campaign is FOR -- the same answer for FEWER runs:")
    print(f"  repetitions per cell : mean {reps.mean():.2f}, range {reps.min()}..{reps.max()}")
    print(f"  runs spent           : {spent}")
    print(f"  a flat 3 reps/cell   : {flat}   ({spent - flat:+d} runs)")
    print("  The saving is real only if the worst crossing is as deep as the campaign this")
    print("  is read against found with MORE runs. Compare both numbers, never one.")
"""

MINIMAX_ANSWER = """\
    outer = [c for c in ('inflation_radius', 'max_speed') if c in scored]
    print("What this campaign is FOR -- which TUNING survives the worst:")
    if outer:
        worst_per_tuning = scored.groupby(outer)['robustness'].min()
        best_tuning = worst_per_tuning.idxmax()
        print(f"  tunings evaluated    : {len(worst_per_tuning)}")
        print(f"  most robust tuning   : " +
              ", ".join(f"{c}={v:.3f}"
                        for c, v in zip(outer, best_tuning if isinstance(best_tuning, tuple)
                                        else (best_tuning,))))
        print(f"  its worst crossing   : {worst_per_tuning.max():.3f}")
        print(f"  the worst tuning's   : {worst_per_tuning.min():.3f}")
        print("  The gap between those two is what the tuning choice is worth.")
"""


# --- what each notebook says -------------------------------------------------

CAMPAIGNS = [
    ("nav_search_random", "Random — the honest denominator", SCATTER,
     """## Random sampling

Uniform draws over the two things that vary: how wide the doorway is, and when the person
is in the way. It assumes nothing and covers everything badly, which is the point — it is
the reference every other strategy here is read against.

**What to look for:** the dots land anywhere, including clumps and gaps. A failure region
narrower than the gaps between them is one this campaign can miss entirely.""",
     COVERAGE_ANSWER),

    ("nav_search_halton", "Halton — the same estimate, tighter", SCATTER,
     """## Low-discrepancy coverage

The same job as `nav_search_random`, done better: a Halton sequence fills the space evenly
by construction rather than by luck.

**What to look for:** compare this picture with the random campaign's at the *same run
budget*. Same number of dots, no clumps, no holes — and the headline's failure fraction
carries less uncertainty for it.""",
     COVERAGE_ANSWER),

    ("nav_search_tpe", "TPE — the single worst crossing", CONVERGENCE,
     """## Exploitation

Optuna's TPE drives toward the most failure-prone combination it can find, and stops once
it has a genuine failure rather than spending the rest of the budget confirming it.

**What to look for:** how *early* the line drops. The comparison with random is not what
was found but how few evaluations found it.""",
     CONVERGENCE_ANSWER),

    ("nav_search_cmaes", "CMA-ES — a different way down", CONVERGENCE,
     """## An evolution strategy

CMA-ES against TPE on the same two dimensions at an equal budget. It also carries the
plateau stop that TPE does not: `no_improvement` ends a search that has stopped learning,
which a fixed batch budget cannot see.

**What to look for:** the same axes as the TPE notebook, so the two curves are directly
comparable. A search that flattens early and keeps spending is the failure this stop
exists to prevent.""",
     CONVERGENCE_ANSWER),

    ("nav_search_qd", "Quality-diversity — how many kinds of trouble", ARCHIVE,
     """## A map, not a maximum

The only campaign here that answers *how many different ways does this go wrong* rather
than *how badly*. Its archive is keyed on the failure mode and on how close the crossing
came, so a cell that collides and a cell that gives up are different entries rather than
two points with the same score.

**What to look for:** how many rows have anything in them. An empty row is a kind of
failure this system does not exhibit — which is a result.""",
     QD_ANSWER),

    ("nav_search_boundary", "Boundary — where it starts failing", SCATTER,
     """## The edge, not the extreme

"Maximize failures" has a trivial answer: close the doorway. The engineering question is
where the edge *is*, and no budget spent deep inside the failure region answers it. This
traces the contour where robustness crosses zero.

**What to look for:** the samples should crowd along the colour change rather than
spreading evenly. That crowding is the strategy working.""",
     BOUNDARY_ANSWER),

    ("nav_search_adaptive_reps", "Adaptive repetitions — the same answer for less", REPS,
     """## Spending where the outcome is in doubt

TPE again, with repetitions allocated by how much nearby cells disagreed rather than
uniformly. Read it against `nav_search_tpe`: same strategy, same space, same *run* budget.

**What to look for:** an uneven bar chart. Cells whose neighbours agreed get one run; cells
near the boundary get several. On the campaign that motivated this, 3 of 32 cells produced
a mixed outcome over 5 repetitions — the other 145 runs each bought a bit that one run had
already established.""",
     REPS_ANSWER),

    ("nav_search_minimax", "Minimax — which tuning survives the worst", FRONT,
     """## Robust design

An outer search over the nav2 settings we choose, and for each one an inner adversary over
the doorway and the walker we do not. A tuning's score is the worst its adversary could
find, so the winner is the one whose bad day is least bad — not the one that looks best on
average.

**What to look for:** `report().extra['robust_tuning']` in the campaign record holds the
answer. The controller's own "best objective" does **not**: it folds the flat inner
objective, which is a different quantity.""",
     MINIMAX_ANSWER),
]


def main():
    for name, title, figure, why, answer in CAMPAIGNS:
        nb = notebook(why, f"TITLE = {title!r}\n\n" + figure, BASE + answer)
        out = HERE / f"{name}.ipynb"
        out.write_text(json.dumps(nb, indent=1) + "\n")
        print("wrote", out.relative_to(HERE.parent))


if __name__ == "__main__":
    main()
