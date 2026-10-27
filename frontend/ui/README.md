# RoboVAST web UI

A plain Vite + React + TypeScript app (MUI + TanStack Query) that is a thin client of
`robovast-service` — the same `RobovastInterface` contract the CLI and MCP server use.

```bash
npm install
npm run build     # -> frontend/ui/dist, served by the service (vast serve)
npm run dev       # Vite dev server on :5173, proxies the API to a running `vast serve`
npm run lint      # tsc --noEmit
npm run test      # vitest over the pure modules in src/lib/ -- keep these minimal
npm run e2e       # the built UI on emulated phones and a desktop, against fixtures (see below)
```

**Keep `npm run test` minimal.** It covers the pure helpers in `src/lib/` and nothing else:
no component/DOM tests, no jsdom, no network mocking, no snapshots, nothing `tsc` already
proves. This is a thin client, so behaviour is tested in the Python suite. The full rule,
and when a spec is worth adding at all, is in *Web UI internals* (`docs/developer_guide.rst`).

**`npm run e2e`** is not a test suite: it serves `dist/` (build first), answers the API from
`e2e/fixtures.mjs`, and drives Playwright's Chromium through every page with a Pixel and an iPhone
profile (portrait and landscape, touch on) and a desktop, writing screenshots and a `report.json`
of pages wider than their viewport and touch targets under 44 px to `e2e/out/`. `--touch` and
`--mouse` drive the run view's 3D scene, `--pwa` checks the installable app, `--strict` fails on
what the probes find, and `--dist` serves another build (to compare before and after). It needs
Playwright's Chromium (`npx playwright install chromium`).

The service serves `frontend/ui/dist` itself, so the UI starts together with `vast serve` (or the
in-cluster service) at the same URL as the REST API — open that URL in a browser.
Point the dev server at another service with `ROBOVAST_SERVICE_URL`.

All service access goes through `src/lib/robovastClient.ts`, which mirrors
`src/robovast/service/interface.py` 1:1 — keep it in sync with the interface.

Full docs: **Web UI** (user guide) and *Web UI internals* in the developer guide
(`docs/web_ui.rst`, `docs/developer_guide.rst`).
