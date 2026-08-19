# Historic campaign fixtures

Two complete campaign directories, frozen at the config versions robovast has actually
shipped, kept so that "an old campaign is still readable and re-runnable" is a property
CI checks rather than a claim someone made once.

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

Neither carries `plugins.yaml` or `providers.yaml`, on purpose: a campaign from before those
records existed must report `unknown` and still be re-runnable. Recording them here would
quietly delete that case from the suite.
