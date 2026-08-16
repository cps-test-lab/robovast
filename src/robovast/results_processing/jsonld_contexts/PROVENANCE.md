# Vendored JSON-LD contexts

These are byte-for-byte copies of the three remote contexts that
`fair_metadata.py` compacts the provenance graph against. They are served from
disk by `_vendored_document_loader()` so that provenance generation does not
depend on two external hosts being reachable.

Vendoring them is not an optimisation detail: `pyld` re-resolves every context on
every operation, and one `generate_prov_metadata()` call performs four of them
(compact → expand → flatten → compact). Fetched over the network that cost ~35 s
per test-suite run and made the suite fail outright when offline.

| File | Source URL | Notes |
|---|---|---|
| `prov.json` | `https://secorolab.github.io/metamodels/prov.json` | 301 → `https://secoro.uni-bremen.de/metamodels/prov.json` |
| `metadata.json` | `https://secorolab.github.io/metamodels/metadata.json` | 301 → `https://secoro.uni-bremen.de/metamodels/metadata.json` |
| `robovast.json` | `https://raw.githubusercontent.com/cps-test-lab/metamodels/refs/heads/main/robovast.json` | pinned below |

Fetched: 2026-08-16.

`robovast.json` is requested at `refs/heads/main`, a **floating** ref, so the copy
here corresponds to a specific commit rather than to whatever `main` points at
today:

    cps-test-lab/metamodels @ 23adba14a42f8029ef484ff8c39859aa8f825812
    ("Simplify successs property", 2026-03-20)

The `secorolab` contexts are served from a site without a comparable version
handle; the fetch date above is their only pin.

## Refreshing

Re-download all three, diff, and update the commit and date above in the same
commit as the content change. None of the three references a further remote
context, so this directory is the complete set — a new nested `@context` URL
appearing upstream would show up as a network fetch at runtime and must be
vendored here too.
