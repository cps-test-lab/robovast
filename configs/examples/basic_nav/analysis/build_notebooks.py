#!/usr/bin/env python3
"""Generate basic_nav's three evaluation notebooks.

They are generated rather than hand-edited because a notebook is JSON with the source
split across a list of lines: reviewing a one-character change to a plot means reading a
diff of escaped strings, and every edit risks producing a file Jupyter will not open.
The Python here is the reviewable form; the ``.ipynb`` beside it is the artifact.

Regenerate with::

    python3 configs/examples/basic_nav/analysis/build_notebooks.py

The contract with the renderer (``robovast.results_processing.notebook_render``): each
notebook must contain a literal ``DATA_DIR = ...`` line, which is replaced with the
absolute path of the node being viewed -- the campaign root, one config dir, or one run
dir -- before every cell is executed and exported to HTML.

**These are deliberately basic.** They exist to exercise the results explorer across the
three deployment shapes (local / cluster / cluster --attach), so they lean on what every
run has -- ``test.xml``, and ``behaviors.jsonl`` when postprocessing produced it -- and
degrade to a printed note rather than a traceback when richer data (poses, rosbags) is
absent. A notebook that fails on missing data cannot tell you whether the *deployment* is
broken, which is the entire question here.
"""

import json
import pathlib

# --- shared cells -----------------------------------------------------------

HEADER = """\
# DATA_DIR is replaced by the service with the node being viewed (campaign, config or
# run directory). The assignment must stay a plain literal for that substitution to work.
DATA_DIR = ''

import os

# No matplotlib.use('Agg') here, tempting as it looks for a headless service: the kernel
# these run in already selects the inline backend, and forcing Agg over it means the
# figures are drawn and then never attached to the cell -- the notebook renders, with
# every plot after the first silently missing.
import matplotlib.pyplot as plt
import pandas as pd

pd.set_option('display.width', 160)
print('data dir:', DATA_DIR)
"""

# Reading a run's own artifacts is done in one place so all three notebooks agree on
# what "a result" is, and so a missing optional file reads the same everywhere.
HELPERS = """\
from robovast.common.analysis import get_run_status, read_run_statuses


def note(message):
    \"\"\"Say what is missing, in the output, instead of raising.

    These notebooks are also a deployment test: a traceback here is indistinguishable
    from the service failing to run notebooks at all, which is the thing under test.
    \"\"\"
    print(f'[no data] {message}')


def behaviors(run_dir):
    \"\"\"A run's behaviour-tree log as a DataFrame, or None if it has none.

    Written directly by scenario_execution (not derived from a rosbag), so it exists
    even for a run whose postprocessing failed -- which is exactly the run you most
    want to look at.
    \"\"\"
    path = os.path.join(run_dir, 'behaviors.jsonl')
    if not os.path.isfile(path):
        return None
    try:
        df = pd.read_json(path, lines=True)
    except ValueError:
        return None
    return df if not df.empty else None
"""


def status_overview(scope: str) -> str:
    """Cell: the pass/fail table, which every scope shows the same way."""
    return f"""\
# Pass/fail comes from each run's test.xml, the one artifact every run writes -- so this
# table is populated even when postprocessing failed and data.db is absent.
statuses = read_run_statuses(DATA_DIR)
if statuses.empty:
    note('no test.xml under DATA_DIR - has any run finished?')
else:
    print(f'{{len(statuses)}} run(s) in this {scope}')
    display(statuses)

    counts = statuses['status'].value_counts()
    fig, ax = plt.subplots(figsize=(5, 3))
    colors = {{'passed': '#2f7d31', 'failed': '#c9611e', 'unknown': '#9e9e9e'}}
    ax.bar(counts.index, counts.values,
           color=[colors.get(s, '#9e9e9e') for s in counts.index])
    ax.set_ylabel('runs')
    ax.set_title('Run outcomes')
    for i, v in enumerate(counts.values):
        ax.text(i, v, str(v), ha='center', va='bottom')
    plt.tight_layout()
    plt.show()
"""


DURATION_BY_CONFIG = """\
# Duration per run, grouped by config. Read from the JUnit testcase time, so it needs no
# rosbag -- the point of comparison for basic_nav is "did this goal take longer".
import xml.etree.ElementTree as ET


def run_duration(run_dir):
    path = os.path.join(run_dir, 'test.xml')
    if not os.path.isfile(path):
        return None
    try:
        root = ET.parse(path).getroot()
        suite = root if root.tag == 'testsuite' else root.find('testsuite')
        case = suite.find('testcase') if suite is not None else None
        return float(case.get('time')) if case is not None and case.get('time') else None
    except Exception:
        return None


rows = []
for dirpath, _dirnames, filenames in os.walk(DATA_DIR):
    if 'test.xml' not in filenames:
        continue
    rows.append({
        'config': os.path.basename(os.path.dirname(dirpath)),
        'run': os.path.basename(dirpath),
        'duration_s': run_duration(dirpath),
        'status': get_run_status(dirpath)[0],
    })

# Columns given explicitly: pd.DataFrame([]) has NO columns, so dropna(subset=...) on it
# raises KeyError rather than yielding an empty frame -- and a campaign with no finished
# run yet is exactly what the explorer shows when someone opens it early.
durations = pd.DataFrame(rows, columns=['config', 'run', 'duration_s', 'status'])
durations = durations.dropna(subset=['duration_s'])
if durations.empty:
    note('no run durations found in any test.xml')
else:
    display(durations.sort_values(['config', 'run']))
    fig, ax = plt.subplots(figsize=(8, 4))
    for config, group in durations.groupby('config'):
        ax.plot(group['run'], group['duration_s'], marker='o', label=config)
    ax.set_xlabel('run')
    ax.set_ylabel('duration (s)')
    ax.set_title('Navigation duration per run')
    if durations['config'].nunique() > 1:
        ax.legend(title='config')
    plt.tight_layout()
    plt.show()
"""


RUN_DETAIL = """\
# One run: its outcome, and the behaviour tree it actually executed.
status, summary = get_run_status(DATA_DIR)
print('status :', status)
if summary:
    print('reason :', summary)

df = behaviors(DATA_DIR)
if df is None:
    note('no behaviors.jsonl - the scenario did not run, or wrote nothing')
else:
    # scenario_execution names the column `behavior_name`; `name` is accepted too so this
    # keeps working if the log schema is ever simplified.
    name_col = 'behavior_name' if 'behavior_name' in df.columns else (
        'name' if 'name' in df.columns else None)
    cols = [c for c in (name_col, 'status', 'feedback_message', 'timestamp')
            if c and c in df.columns]
    display(df[cols].tail(40) if cols else df.tail(40))

    if name_col and 'status' in df.columns:
        # The first row is the log's header record (status NaN), and INVALID means a
        # branch the tree never entered -- neither says anything about where time went.
        ticks = df[df['status'].astype(str).str.upper().isin(['RUNNING', 'SUCCESS'])]
        if ticks.empty:
            note('no RUNNING/SUCCESS ticks recorded')
        else:
            top = ticks[name_col].value_counts().head(12)
            fig, ax = plt.subplots(figsize=(8, max(3, 0.35 * len(top))))
            ax.barh(top.index[::-1], top.values[::-1], color='#2e9599')
            ax.set_xlabel('ticks recorded')
            ax.set_title('Behaviours the run executed')
            plt.tight_layout()
            plt.show()
"""


POSES_IF_PRESENT = """\
# Trajectory, when postprocessing produced poses.csv. Optional on purpose: it is absent
# for a run whose rosbag conversion failed, and that must not hide the tables above.
from robovast.common.analysis import read_output_csv, read_output_files

try:
    poses = read_output_files(DATA_DIR, lambda d: read_output_csv(d, 'poses.csv'))
except Exception as exc:  # noqa: BLE001 - optional data; say so and carry on
    poses = None
    note(f'no poses.csv ({type(exc).__name__}) - run postprocessing to populate it')

# rosbag-derived poses flatten the ROS message, so the columns are `position.x` /
# `position.y`; plain `x`/`y` is accepted as a fallback.
xcol = next((c for c in ('position.x', 'x') if poses is not None and c in poses.columns), None)
ycol = next((c for c in ('position.y', 'y') if poses is not None and c in poses.columns), None)

if poses is not None and not poses.empty and xcol and ycol:
    # Several frames are recorded (odom, amcl, ground truth). Ground truth is the one
    # worth plotting: it is where the robot WAS, not where it believed it was.
    if 'frame' in poses.columns:
        gt = [f for f in poses['frame'].unique() if str(f).endswith('_gt')]
        if gt:
            poses = poses[poses['frame'] == gt[0]]
    fig, ax = plt.subplots(figsize=(7, 7))
    key = next((c for c in ('config', 'run') if c in poses.columns), None)
    if key and poses[key].nunique() > 1:
        for name, group in poses.groupby(key):
            ax.plot(group[xcol], group[ycol], linewidth=1, label=str(name))
        ax.legend(title=key, fontsize='small')
    else:
        ax.plot(poses[xcol], poses[ycol], linewidth=1, color='#2e9599')
    ax.plot(poses[xcol].iloc[0], poses[ycol].iloc[0], 'o', color='#2f7d31', label='start')
    ax.plot(poses[xcol].iloc[-1], poses[ycol].iloc[-1], 'x', color='#c9611e', label='end')
    ax.set_aspect('equal', 'datalim')
    ax.set_xlabel('x (m)')
    ax.set_ylabel('y (m)')
    ax.set_title('Robot trajectory (ground truth)')
    plt.tight_layout()
    plt.show()
elif poses is not None:
    note(f'poses.csv has no usable position columns: {list(poses.columns)[:8]}')
"""


# nbformat >= 4.5 requires a stable per-cell id; without one every load warns, and a
# future version makes it an error. Derived from the position so regenerating an
# unchanged notebook produces an identical file (no diff noise).
_n = iter(range(1000))


def md(text):
    return {"cell_type": "markdown", "id": f"md{next(_n)}", "metadata": {},
            "source": text.splitlines(True)}


def code(text):
    return {"cell_type": "code", "id": f"code{next(_n)}",
            "execution_count": None, "metadata": {}, "outputs": [],
            "source": text.splitlines(True)}


def notebook(cells):
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python",
                           "name": "python3"},
            "language_info": {"name": "python", "version": "3.12"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


NOTEBOOKS = {
    "analysis_campaign.ipynb": notebook([
        md("# Campaign overview\n\nEvery run in this campaign: what passed, and how long "
           "each took.\n"),
        code(HEADER), code(HELPERS),
        md("## Outcomes\n"), code(status_overview("campaign")),
        md("## Duration\n"), code(DURATION_BY_CONFIG),
        md("## Trajectories\n"), code(POSES_IF_PRESENT),
    ]),
    "analysis_config.ipynb": notebook([
        md("# Configuration\n\nThe runs of one configuration -- i.e. one goal pose -- and "
           "how consistent they were.\n"),
        code(HEADER), code(HELPERS),
        md("## Outcomes\n"), code(status_overview("configuration")),
        md("## Duration\n"), code(DURATION_BY_CONFIG),
        md("## Trajectories\n"), code(POSES_IF_PRESENT),
    ]),
    "analysis_run.ipynb": notebook([
        md("# Run\n\nA single navigation run: its outcome and the behaviour tree that "
           "produced it.\n"),
        code(HEADER), code(HELPERS),
        md("## Outcome\n"), code(RUN_DETAIL),
        md("## Trajectory\n"), code(POSES_IF_PRESENT),
    ]),
}


def main():
    here = pathlib.Path(__file__).resolve().parent
    for name, nb in NOTEBOOKS.items():
        path = here / name
        path.write_text(json.dumps(nb, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(here.parent)}")


if __name__ == "__main__":
    main()
