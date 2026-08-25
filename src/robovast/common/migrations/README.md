# Migrations

Everything about versioning and migration in robovast starts here. If you were asked to
"add a migration step", this file tells you which surface you mean and exactly what to do.

## The four version surfaces

| surface | constant | where the steps live | migrates |
|---|---|---|---|
| `.vast` config | `SUPPORTED_CONFIG_VERSION` (`config/__init__.py`) | `config/vN_to_vM.py` | forward, on read |
| campaign store (`campaign.db`) | `SCHEMA_VERSION` (`../store.py`) | `../store.py`, beside `_SCHEMA` | forward, on open |
| analysis DB | `DATA_DB_SCHEMA_VERSION` (`../analysis/db.py`) | `../analysis/db.py` | forward, on open |
| host ↔ container protocol | `COMPAT_VERSION` (`../execution.py`) | not a ladder — a supported window | n/a |

`registry.py` enumerates them programmatically, so nothing has to be kept in sync by hand.

**Why the sqlite ladders are not in this package.** `store.py`'s `_MIGRATIONS` has to sit
beside `_SCHEMA`: a new column must be mirrored into both *in the same order*, and
`test_fresh_and_migrated_schemas_match` exists to catch a drift between them. Moving the
ladder away from the schema would break the coupling that test protects. Finding everything
in one place does not require moving everything into one place — so this file is the map.

## Adding a `.vast` config migration step

```sh
make new-config-migration        # scaffolds the step file, the list entry, and a golden
```

Then implement the transform in the generated `config/vN_to_vM.py`.

### The two rules

1. **A step is a pure `dict -> dict` and must never import `robovast.common.config`.**
   This is the rule that keeps old steps correct. A step that reads the *current* model
   silently changes meaning the next time the model changes, so a migration written today
   would quietly produce something different a year from now. Enforced by
   `test_migration_purity`, which AST-parses every step file.
2. **Append only. Never edit an existing step.** An edit changes what an
   already-migrated campaign becomes. Each step has a golden fixture in `fixtures/`
   precisely so an edit fails loudly.
3. **`copy.deepcopy(raw)` and mutate the copy. Never `dict(raw)`.** When the caller is
   `upgrade_config_file` the mapping is a ruamel `CommentedMap` holding the author's
   comments, and `dict(mapping)` returns a plain dict — silently dropping every comment in
   a file a human is about to edit. `deepcopy` keeps them. (Comments attached to a key the
   step *removes* go with it; that is unavoidable and fine.) Covered by
   `test_upgrade_config_file_preserves_comments`.

### When to bump the version at all

Bump **only** when an existing valid `.vast` would stop meaning what it meant:

| change | bump? |
|---|---|
| added an **optional** field | **no** — old configs stay valid and unchanged |
| renamed a field | prefer `AliasChoices` (accept both names) over a step |
| removed a field, added a **required** field, changed semantics | yes |

An unnecessary bump is a CI failure, not a style nit: `tools/check_config_version.py`
classifies the diff of the committed field snapshot and refuses a bump that the schema
change does not justify.

### Three kinds of breaking change

- **Mechanical restructuring** — keys move, fold, or are renamed. A step handles it.
- **A capability was removed** — nothing to map onto. The step must **refuse**, and how it
  refuses is a contract:

  ```python
  sut["resources"] = migration_marker(
      "'GaussianVariation' was removed in version 4; choose a replacement",
      was=sut.get("resources"))          # what was there, for whoever has to decide
  ...
  raise UnmigratableConfig("...", partial=out, reached=3, capability="GaussianVariation")
  ```

  Two obligations. Leave a `migration_marker()` **wherever** the thing was — it is invalid by
  construction, so the file cannot validate and therefore cannot be launched from, which is the
  point: a config that loads cleanly but runs a *different experiment* is the worst outcome
  available. And raise with `partial=` holding the config migrated as far as you got, because the
  caller's useful move is to hand that to a person, not to report a dead end.

  **Never silently drop a key.** From there, `vast exec retrigger <campaign> --to-workspace <name>`
  materialises the partial config in a workspace with every outstanding decision marked, to finish
  with the ordinary authoring tools. The other fallback is to check out the `robovast_revision` the
  campaign's results recorded and run it there — the ladder and that recorded revision are
  complements, not alternatives.
- **Behaviour changed with no config change** — a default, a timestep, a metric definition.
  Versioning cannot see this and no step helps. The defence is that a campaign's image is
  pinned by digest and reused rather than rebuilt.

## Reading an old config

Three policies, all in `__init__.py`. Pick by what the caller is doing, not by convenience:

| policy | used by | old version |
|---|---|---|
| strict | authoring, launching a **new** campaign | rejected |
| subsection | reading one section (e.g. `results_processing`) | warn only |
| upgrade-in-memory | displaying/importing/retriggering an **archived** campaign | laddered in memory |

`upgrade_config()` takes a dict and is what every reading path uses.
`upgrade_config_file()` preserves comments (`ruamel.yaml`) and is only for rewriting a file
a human will then edit.

**An archived `_config/*.vast` is never rewritten.** It is the record of what the author
wrote; a retrigger migrates its *staging copy* instead.
