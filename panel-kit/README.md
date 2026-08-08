# @robovast/panel-kit

The single source of the run-view **panel contract** (`PanelProps`, `PanelSpec`, `DataProvider`,
`PlaybackClock`) and the **clock-driven scaffolding** every time-synced panel needs
(`useCanvasClock`, the time-index binary search, `useKeyframePump`).

It exists because the host UI (`ui/`) and package-provided panels (`src/robovast_nav/web/`, built as
Module-Federation remotes) are two separately-built npm packages that must agree on one contract. The
remote shares only `react`/`react-dom` with the host at runtime, so it cannot import the host's modules
— before this package, `robovast_nav/web/src/contract.ts` was a **hand-maintained copy** of the host's
types, and the costmap panel re-implemented the host's canvas/clock scaffolding and nearest-sample
lookup. Both drifted, and the drift is what let a staleness bug exist in one panel and nowhere else.

## How it is consumed

Not as an installed dependency: both consumers resolve it **by path**, via a `tsconfig` `paths` entry
plus a matching vite `resolve.alias`. There is no `npm install` step, no `node_modules` symlink, and no
build of its own — each consumer compiles these `.ts` sources through its own vite/esbuild pipeline and
typechecks them with its own `tsc --noEmit`.

Deliberately **not** a Module-Federation `shared` module. Each side bundling its own copy means a
version skew between an installed `robovast_nav` and a newer host UI is a normal (bundled, consistent)
build, not a remote-load failure — the risk the `requiredVersion` pin on react in
`src/robovast_nav/web/vite.config.ts` already warns about. Duplication in the *bundle* is fine;
duplication in the *tree* is what this package removes.

## What belongs here

Only what a **panel** consumes, and only what is free of host-specific machinery:

- `panel.ts` — `PanelSpec` / `PanelProps` / `PanelBuiltins`. The panel-facing spec is `type`, `title`
  and `config`; the host extends it with layout and remote-descriptor fields that no panel reads.
- `clock.ts` — `PlaybackClock` and `useClock`.
- `dataProvider.ts` — the `DataProvider` **interface** and its option types. The implementation
  (`dbDataProvider`, React Query, SQL) stays in the host.
- `useCanvasClock.ts` — HiDPI canvas + rAF-coalesced redraw on clock change and resize.
- `timeIndex.ts` — the sorted-time binary search shared by every nearest-sample lookup.
- `keyframes.ts` — for samples too large to preload: per-frame validity intervals and the
  trailing-edge fetch pump.

Anything that needs `@/lib/*`, React Query, MUI, or the layout engine belongs in the host instead.
