# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Which commit of our own code an image bakes is pinned, and pinned in a refreshable shape.

`test_build_pins` covers the third-party ground a build starts from -- base digests, dated apt
archives. This covers the other half: the images clone roqsim, scenario-execution and the
scenario-execution-server, and each of those clones is a decision about what the image *does*.

The shape is load-bearing, not cosmetic. `tools/refresh_source_pins.py` finds a pin by looking for
`ARG <NAME>_REF` with `ARG <NAME>_REPO` beside it, so a source pinned any other way -- a sha written
inline in the RUN that clones it, say -- is one that no command refreshes and no label records.
It then goes stale invisibly, while `make
release-images-update-versions` reports that everything is current.

Textual for the same reason that module gives: reading a pin needs no Docker, no registry and no
minutes, and catches the regression where it is introduced.
"""

import pathlib
import re

import pytest

CONTAINER = pathlib.Path(__file__).resolve().parents[2] / "container"
DOCKERFILES = sorted(p for p in CONTAINER.rglob("Dockerfile*") if "pins" not in p.parts)

_REF = re.compile(r"^ARG\s+(?P<name>[A-Z0-9_]+)_REF=(?P<value>\S*)", re.MULTILINE)
#: What the images bake, so a fourth baked source cannot arrive unpinned and untested.
EXPECTED = {"SCENARIO_EXECUTION", "SCENARIO_EXECUTION_SERVER", "ROQSIM"}


def _pins(path):
    text = path.read_text(encoding="utf-8")
    return text, {m.group("name"): m.group("value") for m in _REF.finditer(text)}


def test_the_source_pin_set_is_what_this_module_thinks_it_is():
    found = set()
    for path in DOCKERFILES:
        found |= set(_pins(path)[1])
    assert found == EXPECTED


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_source_ref_is_a_full_commit_sha(path):
    """A branch or a tag is not a pin.

    A branch moves, and worse, it moves *behind the layer cache*: the clone layer's key is the
    command text plus the ARGs, not the state of the remote, so a branch default serves the tree
    from the first build forever. A short sha is ambiguous by design.
    """
    for name, value in _pins(path)[1].items():
        assert re.fullmatch(r"[0-9a-f]{40}", value), f"{name}_REF in {path.name} is {value!r}"


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_source_ref_has_a_repo_beside_it(path):
    """Without one there is nowhere for the refresh to ask, so the pin is frozen."""
    text, pins = _pins(path)
    for name in pins:
        assert re.search(rf"^ARG\s+{name}_REPO=\S+", text, re.MULTILINE), \
            f"{name}_REF has no {name}_REPO in {path.name}"


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_a_pinned_repo_url_is_written_once(path):
    """The clone must read the ARG, not repeat the URL.

    A second copy is what makes `--build-arg <NAME>_REPO=<fork>` silently do nothing -- the override
    lands on the ARG while the clone keeps using the literal beside it.
    """
    text, pins = _pins(path)
    for name in pins:
        url = re.search(rf"^ARG\s+{name}_REPO=(\S+)", text, re.MULTILINE)
        if url:
            assert text.count(url.group(1)) == 1, \
                f"{url.group(1)} appears more than once in {path.name}"
