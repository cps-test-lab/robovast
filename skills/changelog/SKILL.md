---
name: changelog
description: Use before a RoboVAST release, when asked for the changelog or the release notes, or what changed since the last version — writes the version's section of CHANGELOG.md as at most fifteen short entries on the changes a user must know about, drawn from the merges since the last tag, and leaves it for the user to review before anything is committed.
---

# The changelog for a release

`CHANGELOG.md` holds one section per released version and is the one place in this
repository where history lives. Its section is the release notes, written for a user of the
previous version: the few changes they must know about, each in a line. Everything else is in
the git history, and the section does not try to be it.

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
header if it does not exist). The section is one flat list, with no headings and no prose:

```markdown
- **Local lane removed** — campaigns run on a cluster; one machine uses minikube (#675)
- **Workspace archives** — download, share and re-create a workspace as one file (#657)
```

- **At most fifteen entries.** Choose by what a user of the previous version has to know:
  first what they must act on (something removed, a setting now refused, a changed default),
  then what they can now do, then behaviour that now differs. Several merges that make one
  change are one entry.
- **An entry is a bold topic of a few words, a dash, and one line on what changed** — at
  most 160 characters before its citations. Present tense, the class of the change and its
  mechanism, never how it was built.
- **Left out:** refactors, tests, CI, lint, documentation-only changes, pin bumps, and fixes
  to something introduced since the last tag. There is no Internal entry.
- **Citations are optional**: `(#675)` or `(#638, #639)` at the end, merges only. A number
  that is not a merge in the range is refused as a typo.

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
