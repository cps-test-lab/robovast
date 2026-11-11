// Answers the SQL the UI posts to `/campaigns/{id}/query` from in-memory tables, by pattern.
//
// Not a SQL engine: it recognises the shapes the UI's own query builders write
// (src/lib/panels/dataProvider.ts, src/lib/resultsTree.ts, src/components/runLog/useRunLog.ts) --
// the run scope, a time window, a column match, MIN/MAX of a time column, DISTINCT of a column, the
// row nearest a time, ORDER BY, LIMIT/OFFSET -- and answers anything else with the scoped rows of
// the table it names. A table the fixtures lack is answered as the service answers it: a 400 whose
// detail is DuckDB's catalog error plus the line robovast_data/engine.py:_explain appends.

/** A table: `columns` as `[name, type]` pairs, `rows` as objects keyed by column name. */
export function table(columns, rows) {
  return { columns, rows }
}

export class SqlError extends Error {
  constructor(detail) {
    super(detail)
    this.detail = detail
  }
}

const NUM = String.raw`-?\d+(?:\.\d+)?(?:e[-+]?\d+)?`
const ident = (s) => s.replace(/^"|"$/g, '')
const asNumber = (v) => (v === null || v === undefined || v === '' ? NaN : Number(v))

function missingTable(name, tables) {
  const known = Object.keys(tables).sort().join(', ')
  return new SqlError(
    `Catalog Error: Table with name ${name} does not exist!\n` +
      `Named: ${name}. A table exists once a run in scope recorded something for it. ` +
      `Defined here: ${known}.`,
  )
}

/** The rows of `tables` the statement selects: `{columns, rows}`, or throws `SqlError`. */
export function runSql(sqlIn, tables) {
  // The results tree's run listing carries a scalar subselect over the campaign record for a
  // search's objective direction; the fixture campaigns have no search block, so it is NULL.
  const sql = sqlIn.replace(/\(SELECT config_json[^)]*?FROM campaign\.campaign LIMIT 1\)/i, 'NULL')

  const from = /\bFROM\s+("[^"]+"|[A-Za-z_][\w.]*)/gi
  let name = null
  for (const m of sql.matchAll(from)) {
    name = ident(m[1])
    break
  }
  if (!name) throw new SqlError(`Parser Error: no table in: ${sqlIn}`)
  const t = tables[name]
  if (!t) throw missingTable(name, tables)
  const colNames = t.columns.map(([c]) => c)

  let rows = t.rows
  const eq = (col, val) => {
    rows = rows.filter((r) => String(r[col]) === String(val))
  }
  const cfg = sql.match(/\bconfig_name\s*=\s*'((?:[^']|'')*)'/i)
  if (cfg) eq('config_name', cfg[1].replace(/''/g, "'"))
  const run = sql.match(/\brun_id\s*=\s*(\d+)/i)
  if (run) eq('run_id', run[1])
  for (const m of sql.matchAll(new RegExp(String.raw`CAST\("([^"]+)" AS REAL\)\s*(>=|<=)\s*(${NUM})`, 'gi'))) {
    const [, col, op, v] = m
    const bound = Number(v)
    rows = rows.filter((r) => (op === '>=' ? asNumber(r[col]) >= bound : asNumber(r[col]) <= bound))
  }
  // A column match the provider writes as `"col" = 'v'` or `"col" = 3` (never inside a CAST).
  for (const m of sql.matchAll(new RegExp(String.raw`(?<!CAST\()"([^"]+)"\s*=\s*('(?:[^']|'')*'|${NUM})`, 'g'))) {
    const val = m[2].startsWith("'") ? m[2].slice(1, -1).replace(/''/g, "'") : m[2]
    eq(m[1], val)
  }
  const sev = sql.match(/\bseverity\s+IN\s*\(([^)]*)\)/i)
  if (sev) {
    const allowed = new Set([...sev[1].matchAll(/'([^']*)'/g)].map((m) => m[1]))
    rows = rows.filter((r) => allowed.has(String(r.severity)))
  }
  for (const m of sql.matchAll(/"([^"]+)"\s+IS NOT NULL/gi)) {
    rows = rows.filter((r) => r[m[1]] !== null && r[m[1]] !== undefined)
  }

  const minmax = sql.match(/MIN\(CAST\("([^"]+)" AS REAL\)\)\s+AS\s+lo/i)
  if (minmax) {
    const vals = rows.map((r) => asNumber(r[minmax[1]])).filter(Number.isFinite)
    return {
      columns: ['lo', 'hi'],
      rows: [{ lo: vals.length ? Math.min(...vals) : null, hi: vals.length ? Math.max(...vals) : null }],
    }
  }
  const distinct = sql.match(/SELECT DISTINCT "([^"]+)" AS v\b/i)
  if (distinct) {
    const vals = [...new Set(rows.map((r) => r[distinct[1]]))].sort()
    return { columns: ['v'], rows: vals.map((v) => ({ v })) }
  }
  const nearest = sql.match(new RegExp(String.raw`ORDER BY ABS\(CAST\("([^"]+)" AS REAL\) - (${NUM})\)`, 'i'))
  if (nearest) {
    const col = nearest[1]
    const at = Number(nearest[2])
    rows = [...rows].sort((a, b) => Math.abs(asNumber(a[col]) - at) - Math.abs(asNumber(b[col]) - at))
  } else {
    const timeOrder = sql.match(/ORDER BY CAST\("([^"]+)" AS REAL\)\s*$/i)
    const plainOrder = sql.match(/ORDER BY ([\w", ]+?)(?:\s+LIMIT|\s*$)/i)
    const keys = timeOrder
      ? [timeOrder[1]]
      : plainOrder
        ? plainOrder[1].split(',').map((k) => ident(k.trim().split(/\s+/)[0]))
        : []
    if (keys.length && keys.every((k) => colNames.includes(k))) {
      rows = [...rows].sort((a, b) => {
        for (const k of keys) {
          const x = a[k]
          const y = b[k]
          const nx = asNumber(x)
          const ny = asNumber(y)
          const d = Number.isFinite(nx) && Number.isFinite(ny) ? nx - ny : String(x).localeCompare(String(y))
          if (d) return d
        }
        return 0
      })
    }
  }

  // The outermost projection, when it is a plain list of columns; anything else gets them all.
  let columns = colNames
  const select = sql.match(/^\s*SELECT\s+(.*?)\s+FROM\b/is)
  if (select && select[1] !== '*') {
    const parts = select[1].split(',').map((p) => p.trim())
    const picked = []
    for (const p of parts) {
      const asMatch = p.match(/^(.*?)\s+AS\s+("?[\w.]+"?)$/i)
      const expr = asMatch ? asMatch[1] : p
      const out = asMatch ? ident(asMatch[2]) : ident(p)
      if (expr.toUpperCase() === 'NULL') picked.push([out, null])
      else if (colNames.includes(ident(expr))) picked.push([out, ident(expr)])
      else {
        picked.length = 0
        break
      }
    }
    if (picked.length) {
      columns = picked.map(([out]) => out)
      rows = rows.map((r) => Object.fromEntries(picked.map(([out, src]) => [out, src === null ? null : r[src]])))
    }
  }

  const limit = [...sql.matchAll(/\bLIMIT\s+(\d+)(?:\s+OFFSET\s+(\d+))?/gi)].pop()
  if (limit) {
    const offset = Number(limit[2] ?? 0)
    rows = rows.slice(offset, offset + Number(limit[1]))
  }
  return { columns, rows }
}
