// Preview run rows: a running campaign's runs, derived from its output directories.
//
// A campaign that is still running has NO rows in the index — they are written by postprocessing
// (`index_query.missing_campaign_note` says so outright) — so `CAMPAIGN_RUNS_SQL` answers nothing for
// one, and the Run view's preview has to learn its runs from the campaign's own output tree instead.
//
// The rows this produces carry the **same column names** `CAMPAIGN_RUNS_SQL` selects, which is the
// whole design: `buildCampaignChildren`, `resolveSelection`, `firstRunSelection` and the node-id
// grammar in `resultsTree.ts` then work unchanged, and the preview picker is the same tree the
// Explorer draws rather than a second one that would have to spell that id grammar again.
//
// Walked one level at a time, never recursively. A recursive listing of this space returns every FILE
// beneath the address (`file_view.scan_dir`), so over a campaign of thousands of runs it is tens of
// thousands of paths — megabytes, and a full prefix listing on the cluster — to learn a few dozen
// directory names. One level is a handful of names, and the caller fetches a configuration's runs only
// when its node is expanded.

import type { NodeStatus } from './resultsTree'

/** Campaign-level directories that are not configurations: the campaign's own machinery, listed
 *  beside its configurations at the root (see the campaign layout in the service's file-listing
 *  docs). A dot-prefixed name is machinery too (`.cache`).
 *
 *  Underscore-prefixed by convention, and matched by name rather than by that prefix so a
 *  configuration someone names with a leading underscore is still listed. `runIds` is the real
 *  safety net either way: none of these holds numbered run directories, so one that slipped
 *  through would contribute no runs rather than a wrong branch. */
const RESERVED = new Set(['_config', '_execution', '_transient', '_jobs'])

const isDir = (entry: string) => entry.endsWith('/')

/** Directory names at one listed level, without their trailing `/`. Files are dropped: this space
 *  lists both, and only directories carry the tree further. */
const dirNames = (entries: string[]): string[] =>
  entries.filter(isDir).map((e) => e.slice(0, -1))

/** The configuration directories of a campaign root listing. */
export function configDirs(entries: string[]): string[] {
  return dirNames(entries).filter((n) => !RESERVED.has(n) && !n.startsWith('.'))
}

/** The run ids of one configuration's listing, ascending.
 *
 *  A run directory is named by its number and nothing else, so anything non-numeric here is some
 *  other artifact of the configuration and not a run. Sorted numerically rather than by name, or
 *  `10` would sit between `1` and `2` and the picker would disagree with every other listing of the
 *  same runs. */
export function runIds(entries: string[]): number[] {
  return dirNames(entries)
    .filter((n) => /^\d+$/.test(n))
    .map(Number)
    .sort((a, b) => a - b)
}

/** A run's verdict is unknowable in preview. `unknown` is the existing `NodeStatus` for exactly this
 *  ("no verdict yet"), and it is a fact rather than a placeholder: pass/fail lives in `campaign.db`,
 *  which is not reachable without the index, so claiming one either way would be inventing it. */
const PREVIEW_STATUS: NodeStatus = 'unknown'

/** Rows for one campaign's preview tree, in the shape `CAMPAIGN_RUNS_SQL` returns.
 *
 *  `batch` is null because a batch is a grouping the index records, not a directory — a search
 *  campaign's rounds are simply not visible here, and the tree drops the batch level rather than
 *  inventing rounds. The two objective columns are null for the same reason. */
export function previewRunRows(runsByConfig: Map<string, number[]>): Record<string, unknown>[] {
  const rows: Record<string, unknown>[] = []
  for (const configName of [...runsByConfig.keys()].sort()) {
    for (const runId of runsByConfig.get(configName) ?? []) {
      rows.push({
        config_name: configName,
        run_id: runId,
        status: PREVIEW_STATUS,
        passed: null,
        objective: null,
        batch: null,
        objective_direction: null,
      })
    }
  }
  return rows
}

/** Whether a campaign's run view has anything a PREVIEW could show: a 3D scene.
 *
 *  A preview replays a run's own recording, and only the `scene3d` panel reads one — every other
 *  panel needs the index, which holds nothing for a campaign that is still running. So a campaign
 *  whose simulator contributes no scene has nothing to preview at all, and is not offered until it
 *  finishes and its real results exist. `scene3d` is contributed by the backend that records the
 *  capture rather than declared in the `.vast`, which is why this asks the campaign's served panel
 *  list rather than reasoning about its configuration.
 *
 *  `undefined` while the list is still being fetched, so a caller can wait rather than guess: both
 *  guesses are wrong in a way somebody sees — offering a campaign that then has nothing to show, or
 *  hiding one that appears a moment later. */
export function declaresScene3d(
  panels: Record<string, unknown>[] | undefined,
): boolean | undefined {
  return panels === undefined ? undefined : panels.some((p) => p.type === 'scene3d')
}
