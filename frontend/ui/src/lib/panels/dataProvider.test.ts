// The SQL a panel's `source` binding turns into. Worth a spec although it is one string builder,
// because every way it can be wrong renders as a *plausible chart* rather than as an error: a
// dropped GROUP BY key silently deletes whole series, a bad `hz` silently rebuckets the run, and the
// decimated shape has to stay byte-identical for the prebuilt costmap remote that already uses it.
import { describe, expect, it } from 'vitest'
import { buildSeriesSql, DECIMATE_ALIAS } from './dataProvider'

const WHERE = "config_name = 'cfg' AND run_id = 3"

describe('buildSeriesSql', () => {
  it('selects everything in time order, with no aggregate, when nothing is asked of it', () => {
    const { sql, sentinel } = buildSeriesSql('poses', WHERE, {})
    expect(sql).toBe(
      `SELECT * FROM "poses" WHERE ${WHERE} ORDER BY CAST("timestamp" AS REAL)`,
    )
    expect(sentinel).toBeNull()
  })

  it('ANDs t0/t1 and match onto the run scope', () => {
    const { sql } = buildSeriesSql('poses', WHERE, {
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

  // The costmap panel remote (src/robovast_nav/web/) is a prebuilt bundle calling this path. Its SQL
  // must not move: aggregate aliased back to the time column, so the row shape is undecimated's.
  it('keeps the columns-given decimation exactly as the remote knows it', () => {
    const { sql, sentinel } = buildSeriesSql('poses', WHERE, {
      columns: ['timestamp', 'position.x', 'position.y'],
      match: { frame: 'base_link' },
      decimate: { hz: 10 },
    })
    expect(sql).toBe(
      'SELECT "position.x", "position.y", MIN(CAST("timestamp" AS REAL)) AS "timestamp" ' +
        `FROM "poses" WHERE ${WHERE} AND "frame" = 'base_link' ` +
        'GROUP BY CAST(CAST("timestamp" AS REAL) * 10 AS INTEGER) ' +
        'ORDER BY CAST("timestamp" AS REAL)',
    )
    expect(sentinel).toBeNull()
  })

  // The vega panel: an author-written spec may read any column, so `SELECT *` stays and the
  // deterministic-pick aggregate rides along under an alias the provider strips again.
  it('hides the aggregate under an alias when the caller wants every column', () => {
    const { sql, sentinel } = buildSeriesSql('poses', WHERE, { decimate: { hz: 5 } })
    expect(sql).toBe(
      `SELECT *, MIN(CAST("timestamp" AS REAL)) AS "${DECIMATE_ALIAS}" ` +
        `FROM "poses" WHERE ${WHERE} ` +
        'GROUP BY CAST(CAST("timestamp" AS REAL) * 5 AS INTEGER) ' +
        'ORDER BY CAST("timestamp" AS REAL)',
    )
    expect(sentinel).toBe(DECIMATE_ALIAS)
  })

  it('groups by the key first on a multi-keyed table, so no series is thinned away', () => {
    const { sql } = buildSeriesSql('poses', WHERE, { decimate: { hz: 2, key: 'frame' } })
    expect(sql).toContain('GROUP BY "frame", CAST(CAST("timestamp" AS REAL) * 2 AS INTEGER)')
  })

  it('honours a non-default time column throughout', () => {
    const { sql } = buildSeriesSql('nav', WHERE, { timeCol: 'sim_time', decimate: { hz: 4 } })
    expect(sql).toContain('MIN(CAST("sim_time" AS REAL))')
    expect(sql).toContain('GROUP BY CAST(CAST("sim_time" AS REAL) * 4 AS INTEGER)')
    expect(sql).toContain('ORDER BY CAST("sim_time" AS REAL)')
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
    const { sql } = buildSeriesSql('poses', WHERE, { decimate: { hz: '2.5' as unknown as number } })
    expect(sql).toContain('CAST(CAST("timestamp" AS REAL) * 2.5 AS INTEGER)')
  })
})
