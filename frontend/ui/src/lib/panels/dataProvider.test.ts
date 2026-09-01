// The SQL a panel's `source` binding turns into. Worth a spec although it is one string builder,
// because every way it can be wrong renders as a *plausible chart* rather than as an error: a
// dropped GROUP BY key silently deletes whole series, a bad `hz` silently rebuckets the run, and the
// decimated shape has to keep one row per bucket for the prebuilt costmap remote that already uses it.
import { describe, expect, it } from 'vitest'
import { buildSeriesSql } from './dataProvider'

const WHERE = "config_name = 'cfg' AND run_id = 3"

describe('buildSeriesSql', () => {
  it('selects everything in time order, with no aggregate, when nothing is asked of it', () => {
    expect(buildSeriesSql('poses', WHERE, {})).toBe(
      `SELECT * FROM "poses" WHERE ${WHERE} ORDER BY CAST("timestamp" AS REAL)`,
    )
  })

  it('ANDs t0/t1 and match onto the run scope', () => {
    const sql = buildSeriesSql('poses', WHERE, {
      t0: 1,
      t1: 9.5,
      match: { frame: 'base_link', level: 2 },
      columns: ['position.x'], // verbatim: adding the time column is the binding's job, not this one's
    })
    expect(sql).toBe(
      `SELECT "position.x" FROM "poses" WHERE ${WHERE} ` +
        'AND CAST("timestamp" AS REAL) >= 1 AND CAST("timestamp" AS REAL) <= 9.5 ' +
        `AND "frame" = 'base_link' AND "level" = 2 ORDER BY CAST("timestamp" AS REAL)`,
    )
  })

  // The costmap panel remote (src/robovast_nav/web/) is a prebuilt bundle calling this path, so the
  // decimated row shape is its contract: one row per bucket, carrying the requested columns.
  it('picks each bucket\'s earliest row when columns are given', () => {
    const sql = buildSeriesSql('poses', WHERE, {
      columns: ['timestamp', 'position.x', 'position.y'],
      match: { frame: 'base_link' },
      decimate: { hz: 10 },
    })
    expect(sql).toBe(
      'SELECT * FROM (SELECT DISTINCT ON (CAST(CAST("timestamp" AS REAL) * 10 AS INTEGER)) ' +
        '"timestamp", "position.x", "position.y" ' +
        `FROM "poses" WHERE ${WHERE} AND "frame" = 'base_link' ` +
        'ORDER BY CAST(CAST("timestamp" AS REAL) * 10 AS INTEGER), CAST("timestamp" AS REAL)) t ' +
        'ORDER BY CAST("timestamp" AS REAL)',
    )
  })

  // The vega panel: an author-written spec may read any column, so `SELECT *` stays. DISTINCT ON
  // returns the row itself, so nothing has to ride along in it and be stripped again.
  it('keeps every column when the caller asks for none in particular', () => {
    const sql = buildSeriesSql('poses', WHERE, { decimate: { hz: 5 } })
    expect(sql).toBe(
      'SELECT * FROM (SELECT DISTINCT ON (CAST(CAST("timestamp" AS REAL) * 5 AS INTEGER)) * ' +
        `FROM "poses" WHERE ${WHERE} ` +
        'ORDER BY CAST(CAST("timestamp" AS REAL) * 5 AS INTEGER), CAST("timestamp" AS REAL)) t ' +
        'ORDER BY CAST("timestamp" AS REAL)',
    )
  })

  it('keys by the series column first on a multi-keyed table, so no series is thinned away', () => {
    const sql = buildSeriesSql('poses', WHERE, { decimate: { hz: 2, key: 'frame' } })
    expect(sql).toContain(
      'DISTINCT ON ("frame", CAST(CAST("timestamp" AS REAL) * 2 AS INTEGER))',
    )
    expect(sql).toContain(
      'ORDER BY "frame", CAST(CAST("timestamp" AS REAL) * 2 AS INTEGER), CAST("timestamp" AS REAL)',
    )
  })

  it('honours a non-default time column throughout', () => {
    const sql = buildSeriesSql('nav', WHERE, { timeCol: 'sim_time', decimate: { hz: 4 } })
    expect(sql).toContain('DISTINCT ON (CAST(CAST("sim_time" AS REAL) * 4 AS INTEGER))')
    expect(sql).toContain('ORDER BY CAST("sim_time" AS REAL)')
  })

  // The time column is what the outer ORDER BY reads out of the subquery, so it has to be selected
  // even when the binding did not ask for it.
  it('adds the time column to the inner select when the caller omitted it', () => {
    const sql = buildSeriesSql('poses', WHERE, {
      columns: ['position.x'],
      decimate: { hz: 1 },
    })
    expect(sql).toContain('"position.x", "timestamp" FROM "poses"')
  })

  // `hz` arrives from a .vast, so every one of these would otherwise be interpolated into the SQL:
  // 0 collapses the run to a single row, a string pastes in as a bare identifier.
  it.each([0, -1, NaN, Infinity, '', 'x', '1) UNION SELECT * FROM sqlite_master --'])(
    'refuses decimate.hz %p rather than building SQL from it',
    (hz) => {
      expect(() =>
        buildSeriesSql('poses', WHERE, { decimate: { hz: hz as number } }),
      ).toThrow(/decimate\.hz must be a finite number > 0/)
    },
  )

  it('accepts a numeric string for hz, since YAML hands one over', () => {
    const sql = buildSeriesSql('poses', WHERE, { decimate: { hz: '2.5' as unknown as number } })
    expect(sql).toContain('CAST(CAST("timestamp" AS REAL) * 2.5 AS INTEGER)')
  })
})
