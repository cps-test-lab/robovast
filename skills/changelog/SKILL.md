---
name: changelog
description: Use before a RoboVAST release, when asked for the changelog or the release notes, or what changed since the last version — writes the version's section of CHANGELOG.md from the merges since the last tag, minimal but complete, and leaves it for the user to review before anything is committed.
---

# The changelog for a release

`CHANGELOG.md` holds one section per released version and is the one place in this
repository where history lives. Its section is the release notes: what a user of the
previous version needs to know, and nothing they do not.

## The range

The previous release is the newest `v*` tag reachable from `origin/main`; the head is
`origin/main`. Fetch first. The version being written is the one the user names — the skill
never chooses a number, so ask if none was given.

```bash
git fetch -q origin main --tags
python3 skills/changelog/changelog.py collect        # v<last>..origin/main
```

`collect` prints one block per merge: its number, its title, the first paragraph of its
description, and the areas of the tree it touched. Read that, not `git log` by hand. A merge
with no pull request number is keyed by its short sha and is cited as that.

## Write the section

A new `## X.Y.Z` section at the top of `CHANGELOG.md` (create the file with its two-line
header if it does not exist). Its parts are `###` headings, in this order, empty ones left
out — Markdown headings rather than bold lines, which the linter refuses as headings:

| heading | holds |
|---|---|
| **Removed** | what a user must act on: a command, tool, option or file that is gone, and what answers it now |
| **Added** | what a user can do that they could not |
| **Changed** | behaviour that differs for the same input |
| **Fixed** | behaviour that was wrong and is right |
| **Images and packaging** | what the images carry, how the distributions install |
| **Internal** | one sentence naming the merges a user cannot see — refactors, CI, lint, pin bumps — that starts with words, since a line starting with `#` reads as a heading |

An entry is one list item, present tense, the way the pull request titles are written: the class
of the change and its mechanism, ending in the merge(s) it cites, as `(#640)` or
`(#638, #639)` when several merges make one change. **Minimal**: an entry per change a
user notices, not per merge; a change that took four pull requests is one entry citing four.
**Full**: every merge in the range is cited exactly once somewhere in the section, which
`check` enforces. Cite merges only, never issues — a number that is not a merge in the
range is refused as a typo.

What never goes in: a hostname, a node, a registry, a cluster's size, a campaign, a run
count or a figure measured on one deployment; who or what wrote the entry.

```bash
python3 skills/changelog/changelog.py check --version X.Y.Z     # must print ok
```

## The user reviews it before it is committed

Show the section (`git diff -- CHANGELOG.md`) and stop. Commit nothing: the user says what
to change, or that it goes in. Then `CHANGELOG.md` is committed on its own, in the pull
request that prepares the release, before any tag.

## The release body

```bash
python3 skills/changelog/changelog.py section --version X.Y.Z
```

prints the section without its heading, for `gh release create vX.Y.Z --notes-file -`.
