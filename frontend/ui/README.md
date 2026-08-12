# RoboVAST web UI

A plain Vite + React + TypeScript app (MUI + TanStack Query) that is a thin client of
`robovast-service` — the same `RobovastInterface` contract the CLI and MCP server use.

```bash
npm install
npm run build     # -> frontend/ui/dist, served by the service (vast serve)
npm run dev       # Vite dev server on :5173, proxies the API to a running `vast serve`
```

The service serves `frontend/ui/dist` itself, so the UI starts together with `vast serve` (or the
in-cluster service) at the same URL as the REST API — open that URL in a browser.
Point the dev server at another service with `ROBOVAST_SERVICE_URL`.

All service access goes through `src/lib/robovastClient.ts`, which mirrors
`src/robovast/service/interface.py` 1:1 — keep it in sync with the interface.

Full docs: **Web UI** (user guide) and *Web UI internals* in the developer guide
(`docs/web_ui.rst`, `docs/developer_guide.rst`).
