import { describe, expect, it } from 'vitest'
import { chartSegments } from './BatchObjectiveChart'
import type { SearchHistory } from '@/lib/robovastClient'

const batch = (idx: number, stage: string, best: number) => ({
  idx, stage, n_units: 2, n_scored: 2, min: best, max: best + 1, mean: best + 0.5,
  best_so_far: best, complete: true,
})

const stage = (idx: number, name: string, objective: string, direction: string) => ({
  idx, name, over: ['x'], objective_name: objective, direction, outcome: 'done',
  stop_kind: null, stop_reason: null, answer: null, region: null, unavailable: null,
})

describe('chartSegments', () => {
  it('draws a search in one stage as one unlabelled chart, as before stages', () => {
    const history = {
      stages: [stage(0, 'search', 'failure_rate', 'maximize')],
      batches: [batch(0, 'search', 0.2), batch(1, 'search', 0.6)],
      unavailable: null,
    } as unknown as SearchHistory
    const [only, ...rest] = chartSegments(history)
    expect(rest).toEqual([])
    expect(only.label).toBeNull()
    expect(only.batches.map((b) => b.idx)).toEqual([0, 1])
    expect(only.stage?.objective_name).toBe('failure_rate')
  })

  it('gives each stage its own segment, objective and rounds, in the order of the file', () => {
    const history = {
      stages: [stage(0, 'obstacle', 'margin', 'minimize'), stage(1, 'speed', 'time', 'maximize')],
      batches: [batch(0, 'obstacle', -1), batch(1, 'obstacle', -2), batch(2, 'speed', 4)],
      unavailable: null,
    } as unknown as SearchHistory
    const segments = chartSegments(history)
    expect(segments.map((s) => s.label)).toEqual(['1/2 obstacle', '2/2 speed'])
    expect(segments.map((s) => s.batches.map((b) => b.idx))).toEqual([[0, 1], [2]])
    expect(segments.map((s) => s.stage?.direction)).toEqual(['minimize', 'maximize'])
  })

  it('says why a history has nothing to draw rather than drawing stages', () => {
    const history = { stages: [], batches: [], unavailable: 'batch_mode' } as unknown as SearchHistory
    expect(chartSegments(history)).toEqual([
      { label: null, stage: undefined, batches: [], unavailable: 'batch_mode' }])
  })
})
