# Historic campaign fixtures

Six complete campaign directories, frozen at the config versions robovast has actually
shipped, kept so that "an old campaign is still readable, and its configuration
re-runnable" is a property CI checks rather than a claim someone made once. None of them fixes
a digest for every image it ran, so a retrigger of one as archived is refused on its images;
the tests give a copy that record to exercise the rest.

Every guarantee built for old campaigns — the migration ladder, the three read policies, the
container-protocol window, the retrigger pre-flight — decays the moment nobody exercises it
against a genuinely old campaign. Nothing else in the suite does: the rest of the tests
construct configs at the *current* version, which is exactly the case that cannot regress.

**These are fixtures, not records.** They are hand-written to be the smallest thing that
exercises the shapes, so they may be edited freely — unlike a real campaign's `_config/`,
which is the record of what its author wrote. What must not change is that they stay at their
declared version: "migrate a v1 campaign" stops being tested the moment the v1 fixture is
helpfully upgraded.

| directory | config version | why it exists |
|---|---|---|
| `v1-campaign-2025-03-04-101500` | 1 | the format before `execution.containers`: `execution.image`, `resources`, and `secondary_containers` in the sibling spelling every real campaign used |
| `v2-campaign-2026-03-04-152130` | 2 | the format on `main` when the ladder was introduced; also carries no provenance records, like every campaign predating them |
| `v3-campaign-2026-08-25-120000` | 3 | `execution.timeout` as the whole job's budget, `bt_log`/`log_topics` gone, `shm_size` defaulted. Declares the keys v3 changed, and packs runs, so a later step that reasons about per-run versus per-job budgets meets a fixture where the two actually differ |
| `v4-campaign-2026-09-07-090000` | 4 | a configuration's fixed values under `parameters:`, grouped by channel, plus a `configuration_presets:` block a configuration composes with `use:`. Declares both, so a later step meets the preset block and not only the entries; also declares `results_processing.resources` and a bare `run_log` step, which v5 removes |
| `v5-campaign-2026-09-23-090000` | 5 | tables built by the decoder: `rosbags_*` entries configure it, and no `results_processing.resources`. Packs runs, so v6 meets a job budget it has to split |
| `v6-campaign-2026-09-25-090000` | 6 | one run per job: no `runs_per_job`, and `timeout` is one run's budget |

None of them carries `plugins.yaml` or `providers.yaml`, on purpose: a campaign from before those
records existed must report `unknown` and still be re-runnable. Recording them here would
quietly delete that case from the suite.
