# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What the local lane records in ``_execution/execution.yaml``.

The local writer emits *shell* into the generated ``run.sh``, so these tests run that shell
and parse what it wrote — the only way to catch a quoting or heredoc mistake, which shows up
as an unparseable file rather than a wrong value.

Why per-role digests matter here: the run view compiles its 3D geometry in the SIMULATION
image, and with only a campaign-level image recorded the reader fell back to the scenario
container's — an image with neither the world nor the exporter.
"""

import subprocess

import pytest
import yaml

from robovast.common.execution import generate_execution_yaml_script

INSPECT_OK = "sha256:" + "a" * 64
INSPECT_OTHER = "sha256:" + "b" * 64


def _run(tmp_path, roles, *, inspectable, main_image="reg/main:1"):
    """Execute the generated shell with a fake ``docker`` on PATH, return the parsed YAML.

    The fake prints an id for images in *inspectable* and otherwise behaves like the real
    one for an image it does not have: an empty line on stdout **and** a non-zero exit.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    known = "\n".join(f'    "{img}") echo "{rev}";;' for img, rev in inspectable.items())
    (bindir / "docker").write_text(
        '#!/bin/bash\n'
        'for a in "$@"; do img="$a"; done\n'
        'case "$img" in\n'
        f'{known}\n'
        '    *) echo ""; exit 1;;\n'
        'esac\n')
    (bindir / "docker").chmod(0o755)

    out = tmp_path / "results"
    script = generate_execution_yaml_script(
        4, execution_params={"env": [{"PYTHONUNBUFFERED": "1"}]},
        output_dir_var=str(out), role_images=roles)
    shell = (f'#!/bin/bash\nset -euo pipefail\nexport PATH="{bindir}:$PATH"\n'
             f'DOCKER_IMAGE={main_image}\n' + script)
    proc = subprocess.run(["bash", "-c", shell], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr[-500:]
    return yaml.safe_load((out / "_execution" / "execution.yaml").read_text())


def test_per_role_images_and_digests_are_recorded(tmp_path):
    doc = _run(tmp_path,
               {"simulation": "reg/sim:1", "scenario": "reg/main:1", "sut": "reg/sut:1"},
               inspectable={"reg/sim:1": INSPECT_OK, "reg/main:1": INSPECT_OTHER,
                            "reg/sut:1": INSPECT_OTHER})
    assert doc["images"] == {"simulation": "reg/sim:1", "scenario": "reg/main:1",
                             "sut": "reg/sut:1"}
    assert doc["image_revisions"]["simulation"] == INSPECT_OK
    assert doc["image_revisions"]["sut"] == INSPECT_OTHER
    # The rest of the document is unchanged by the addition.
    assert doc["execution_type"] == "local" and doc["runs"] == 4
    assert doc["env"] == {"PYTHONUNBUFFERED": "1"} and doc["cluster_info"] == {}


def test_an_uninspectable_image_is_omitted_not_recorded_as_unknown(tmp_path):
    """A recorded non-answer would satisfy the reader's first source and defeat the point.

    Also the regression for the quoting bug this shell had: ``docker inspect`` prints an
    empty line *and* fails for an image it lacks, so ``|| echo unknown`` captured
    ``"\\nunknown"`` — a newline inside a YAML scalar, which made the file unparseable.
    """
    doc = _run(tmp_path, {"simulation": "reg/sim:1", "sut": "reg/gone:1"},
               inspectable={"reg/sim:1": INSPECT_OK})
    assert doc["image_revisions"] == {"simulation": INSPECT_OK}
    assert doc["images"]["sut"] == "reg/gone:1"  # what was asked for is still recorded


def test_the_file_stays_parseable_when_nothing_can_be_inspected(tmp_path):
    doc = _run(tmp_path, {"simulation": "reg/gone:1"}, inspectable={})
    assert doc["image_revisions"] is None  # the key with no entries under it
    assert doc["image_revision"] == "unknown"  # the campaign-level default still applies


def test_without_role_images_the_document_is_unchanged(tmp_path):
    """The pre-existing contract: callers that pass no plan get exactly what they got before."""
    doc = _run(tmp_path, {}, inspectable={"reg/main:1": INSPECT_OK})
    assert "images" not in doc and "image_revisions" not in doc
    assert doc["image_revision"] == INSPECT_OK


@pytest.mark.parametrize("image", ["reg/sim:1", "reg/sim@sha256:" + "c" * 64])
def test_image_references_survive_the_shell_verbatim(tmp_path, image):
    """A ``@sha256:`` ref contains characters the shell would happily mangle."""
    doc = _run(tmp_path, {"simulation": image}, inspectable={image: INSPECT_OK})
    assert doc["images"]["simulation"] == image
