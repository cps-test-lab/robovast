// The rows-backed motion source: what a run's `sim_poses` and `joint_states` rows become for the
// 3D panel, and the two behaviours a file never needed -- paging through a window at the query's
// row cap, and following a run that is still recording.
import { describe, expect, it } from 'vitest'

import { openRowMotion, type Row, type RowEvent, type RowReader } from './rowMotion'

const pose = (t: number, frame: string, x: number): Row => ({
  timestamp: String(t), frame,
  'position.x': String(x), 'position.y': '0', 'position.z': '0',
  'orientation.x': '0', 'orientation.y': '0', 'orientation.z': '0', 'orientation.w': '1',
})
const joint = (t: number, name: string, position: number): Row => ({
  timestamp: t, joint: name, position,
})

/** A reader over fixed rows per table, paging like the service: the first `maxRows` by time. */
function reader(tables: Record<string, Row[]>, log: string[] = []): RowReader {
  return {
    async page(table, t0, t1, maxRows) {
      log.push(`${table}[${t0},${t1}]`)
      const rows = (tables[table] ?? [])
        .filter((r) => Number(r.timestamp) >= t0 && Number(r.timestamp) <= t1)
        .sort((a, b) => Number(a.timestamp) - Number(b.timestamp))
      if (!(table in tables)) throw new Error(`no table ${table}`)
      return { rows: rows.slice(0, maxRows), truncated: rows.length > maxRows }
    },
  }
}

function sink() {
  const joints: Record<string, number> = {}
  const poses: Record<string, number[]> = {}
  return {
    joints, poses,
    joint: (name: string, value: number) => { joints[name] = value },
    pose: (name: string, pos: ArrayLike<number>, quat: readonly number[]) => {
      poses[name] = [...Array.from(pos), ...quat]
    },
  }
}

describe('a finished run', () => {
  const tables = {
    sim_poses: [pose(0, 'base', 0), pose(1, 'base', 1), pose(2, 'base', 2), pose(1, 'prop', 9)],
    joint_states: [joint(0, 'wheel', 0.1), joint(1, 'wheel', 0.2), joint(2, 'wheel', 0.3)],
  }

  it('builds one track per body and per joint, and seats the sample nearest a time', async () => {
    const source = openRowMotion(reader(tables))
    await source.fetch(0, 10)
    expect(source.tracks().map((t) => `${t.kind}:${t.name}`).sort())
      .toEqual(['joint:wheel', 'pose:base', 'pose:prop'])
    expect(source.range()).toEqual({ t0: 0, t1: 2, complete: true })
    const s = sink()
    source.apply(source.indexAt(1.4), s)
    expect(s.joints.wheel).toBeCloseTo(0.2)
    // Position, then the quaternion in the descriptor's wxyz order.
    expect(s.poses.base).toEqual([1, 0, 0, 1, 0, 0, 0])
    expect(s.poses.prop).toEqual([9, 0, 0, 1, 0, 0, 0])
  })

  it('pages through a window at the row cap, without losing the tick a page ends in', async () => {
    const rows: Row[] = []
    for (let t = 0; t < 10; t++) for (const f of ['a', 'b', 'c']) rows.push(pose(t, f, t))
    const log: string[] = []
    const source = openRowMotion(reader({ sim_poses: rows, joint_states: [] }, log), { pageRows: 4 })
    await source.fetch(0, 20)
    // Every tick of every body arrived, though the pages of four cut ticks in half.
    const s = sink()
    for (let t = 0; t < 10; t++) {
      source.apply(source.indexAt(t), s)
      expect([s.poses.a[0], s.poses.b[0], s.poses.c[0]]).toEqual([t, t, t])
    }
    expect(log.filter((l) => l.startsWith('sim_poses')).length).toBeGreaterThan(1)
  })

  it('reads only the part of a window it does not have, and drops what is far from it', async () => {
    const rows: Row[] = []
    for (let t = 0; t <= 400; t++) rows.push(pose(t, 'a', t))
    const log: string[] = []
    const source = openRowMotion(reader({ sim_poses: rows, joint_states: [] }, log), { keepS: 50 })
    await source.fetch(0, 100)
    await source.fetch(50, 150)
    expect(log.filter((l) => l.startsWith('sim_poses'))).toEqual([
      'sim_poses[0,100]', 'sim_poses[100,150]',
    ])
    expect(source.range()).toMatchObject({ t0: 0, t1: 150 })
    await source.fetch(300, 350)
    // Disjoint: loaded afresh, and the old window is gone.
    expect(source.range()).toMatchObject({ t0: 300, t1: 350 })
    expect(source.indexAt(10)).toBe(0)
  })

  it('still drives the table the run has when the other is missing', async () => {
    const source = openRowMotion(reader({ sim_poses: tables.sim_poses }))
    await source.fetch(0, 10)
    expect(source.tracks().map((t) => t.name)).toEqual(['base', 'prop'])
  })

  it('fails when neither table can be read', async () => {
    const source = openRowMotion(reader({}))
    await expect(source.fetch(0, 10)).rejects.toThrow(/no table/)
  })
})

describe('a run still recording', () => {
  function live() {
    const listeners = new Map<string, (e: RowEvent) => void>()
    const tables: Record<string, Row[]> = { sim_poses: [pose(0, 'a', 0)], joint_states: [] }
    const log: string[] = []
    const base = reader(tables, log)
    const r: RowReader = {
      page: base.page,
      follow(table, listener) {
        listeners.set(table, listener)
        return () => listeners.delete(table)
      },
    }
    return {
      reader: r, tables, log,
      push: (table: string, rows: Row[]) => listeners.get(table)!({ kind: 'batch', rows }),
      send: (table: string, event: RowEvent) => listeners.get(table)!(event),
    }
  }

  it('appends the rows that follow the history and tells its subscribers', async () => {
    const l = live()
    const source = openRowMotion(l.reader)
    let heard = 0
    source.subscribe(() => { heard += 1 })
    await source.fetch(0, 10)
    expect(source.range()).toEqual({ t0: 0, t1: 0, complete: false })
    l.push('sim_poses', [pose(1, 'a', 1), pose(2, 'a', 2)])
    expect(source.range()).toMatchObject({ t1: 2 })
    expect(heard).toBe(2)
    const s = sink()
    source.apply(source.indexAt(2), s)
    expect(s.poses.a[0]).toBe(2)
  })

  it('re-reads the window after a gap in the stream', async () => {
    const l = live()
    const source = openRowMotion(l.reader)
    await source.fetch(0, 10)
    l.tables.sim_poses.push(pose(3, 'a', 3))
    l.send('sim_poses', { kind: 'gap' })
    await new Promise((r) => setTimeout(r, 0))
    expect(source.range()).toMatchObject({ t1: 3 })
    expect(l.log.filter((x) => x.startsWith('sim_poses'))).toEqual(['sim_poses[0,10]', 'sim_poses[0,10]'])
  })

  it('is complete once the run ends, after one more read of the finished tables', async () => {
    const l = live()
    const source = openRowMotion(l.reader)
    await source.fetch(0, 10)
    l.tables.sim_poses.push(pose(5, 'a', 5))
    l.send('sim_poses', { kind: 'eof' })
    await new Promise((r) => setTimeout(r, 0))
    expect(source.range()).toEqual({ t0: 0, t1: 5, complete: true })
  })

  it('hears nothing after dispose', async () => {
    const l = live()
    const source = openRowMotion(l.reader)
    await source.fetch(0, 10)
    source.dispose()
    expect(source.indexAt(0)).toBe(-1)
  })
})
