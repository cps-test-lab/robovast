# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""``execution.generate`` produces campaign inputs, and never a stale or partial one.

The properties under test are the ones the feature exists for: a generated file is
indistinguishable from a hand-written one downstream (it joins ``run_files``, so it reaches
the config identity), it is rebuilt when anything it read changes, and every way of failing
is loud rather than leaving an artifact nobody notices is wrong.
"""

import os
import sys
import textwrap

import pytest

from robovast.common.errors import CampaignConfigError
from robovast.common.input_generation import (MANIFEST_NAME, Shell,
                                              collect_output_files,
                                              parse_generate_entry,
                                              read_manifest,
                                              resolve_input_generator,
                                              run_input_generators,
                                              write_manifest)

GENERATOR_SRC = textwrap.dedent("""\
    import os
    from robovast.common.input_generation import BaseInputGenerator, write_manifest

    class Compile(BaseInputGenerator):
        def __call__(self, vast_dir, out_dir, source=None, empty=False, fail=False,
                     manifest=True, **kw):
            if fail:
                raise RuntimeError("compiler exploded")
            if empty:
                return True, "wrote nothing"
            src = os.path.join(vast_dir, source)
            with open(src) as fh:
                body = fh.read()
            with open(os.path.join(out_dir, "scene.json"), "w") as fh:
                fh.write(body)
            if manifest:
                write_manifest(out_dir, [src])
            return True, "compiled"
""")


def _project(tmp_path, body="wall: 1\n"):
    (tmp_path / "gen.py").write_text(GENERATOR_SRC)
    (tmp_path / "world.yaml").write_text(body)
    return tmp_path


def _entry(**params):
    params.setdefault("out", "files/scene")
    params.setdefault("source", "world.yaml")
    return [{"./gen.py:Compile": params}]


# -- the happy path ---------------------------------------------------------------


def test_generates_into_out_and_reports_outputs(tmp_path):
    _project(tmp_path)
    records = run_input_generators(str(tmp_path), _entry())
    assert records[0]["outputs"] == ["files/scene/scene.json"]
    assert (tmp_path / "files/scene/scene.json").read_text() == "wall: 1\n"


def test_manifest_is_not_shipped_as_a_campaign_input(tmp_path):
    """The manifest is robovast bookkeeping and holds host-absolute paths."""
    _project(tmp_path)
    run_input_generators(str(tmp_path), _entry())
    assert (tmp_path / "files/scene" / MANIFEST_NAME).is_file()
    assert collect_output_files(str(tmp_path / "files/scene"), str(tmp_path)) == [
        "files/scene/scene.json"]


def test_provenance_records_input_hashes(tmp_path):
    _project(tmp_path)
    records = run_input_generators(str(tmp_path), _entry())
    inputs = records[0]["inputs"]
    assert [os.path.basename(i["path"]) for i in inputs] == ["world.yaml"]
    assert inputs[0]["sha256"]


# -- staleness --------------------------------------------------------------------


def test_unchanged_inputs_skip_regeneration(tmp_path):
    _project(tmp_path)
    run_input_generators(str(tmp_path), _entry())
    again = run_input_generators(str(tmp_path), _entry())
    assert again[0]["cached"] is True


def test_changed_input_regenerates(tmp_path):
    _project(tmp_path)
    run_input_generators(str(tmp_path), _entry())
    (tmp_path / "world.yaml").write_text("wall: 2\n")
    again = run_input_generators(str(tmp_path), _entry())
    assert again[0]["cached"] is False
    assert (tmp_path / "files/scene/scene.json").read_text() == "wall: 2\n"


def test_deleted_output_regenerates_despite_a_cache_stamp(tmp_path):
    """A stamp must not vouch for an artifact that is no longer there."""
    _project(tmp_path)
    run_input_generators(str(tmp_path), _entry())
    (tmp_path / "files/scene/scene.json").unlink()
    again = run_input_generators(str(tmp_path), _entry())
    assert again[0]["cached"] is False
    assert (tmp_path / "files/scene/scene.json").is_file()


def test_generator_without_a_manifest_is_never_cached(tmp_path):
    """No manifest means "cannot tell what I read" — which must not become "unchanged"."""
    _project(tmp_path)
    run_input_generators(str(tmp_path), _entry(manifest=False))
    again = run_input_generators(str(tmp_path), _entry(manifest=False))
    assert again[0]["cached"] is False


# -- failure is always loud -------------------------------------------------------


def test_unknown_generator_names_the_environment(tmp_path):
    with pytest.raises(CampaignConfigError) as excinfo:
        run_input_generators(str(tmp_path), [{"nope": {"out": "x"}}])
    message = str(excinfo.value)
    assert "nope" in message and "Environment:" in message


def test_generator_writing_nothing_is_an_error(tmp_path):
    _project(tmp_path)
    with pytest.raises(CampaignConfigError, match="wrote no files"):
        run_input_generators(str(tmp_path), _entry(empty=True))


def test_failure_leaves_the_previous_artifact_intact(tmp_path):
    """A broken rebuild must not destroy the artifact that was working."""
    _project(tmp_path)
    run_input_generators(str(tmp_path), _entry())
    with pytest.raises(CampaignConfigError, match="compiler exploded"):
        run_input_generators(str(tmp_path), _entry(fail=True, source="world.yaml"))
    assert (tmp_path / "files/scene/scene.json").read_text() == "wall: 1\n"


def test_out_may_not_escape_the_project(tmp_path):
    _project(tmp_path)
    with pytest.raises(CampaignConfigError):
        run_input_generators(str(tmp_path), _entry(out="../outside"))


def test_missing_out_is_an_error(tmp_path):
    _project(tmp_path)
    with pytest.raises(CampaignConfigError, match="must declare 'out'"):
        run_input_generators(str(tmp_path), [{"./gen.py:Compile": {"source": "world.yaml"}}])


def test_two_generators_may_not_share_an_output_dir(tmp_path):
    _project(tmp_path)
    with pytest.raises(CampaignConfigError, match="already written by"):
        run_input_generators(str(tmp_path), _entry() + _entry())


# -- entry shapes and the shell built-in ------------------------------------------


@pytest.mark.parametrize("entry,expected", [
    ("shell", ("shell", {})),
    ({"shell": None}, ("shell", {})),
    ({"shell": {"out": "x"}}, ("shell", {"out": "x"})),
])
def test_parse_generate_entry_shapes(entry, expected):
    assert parse_generate_entry(entry, 0) == expected


@pytest.mark.parametrize("entry", [{"a": {}, "b": {}}, {"a": "not-a-mapping"}, 42])
def test_parse_generate_entry_rejects_bad_shapes(entry):
    with pytest.raises(CampaignConfigError):
        parse_generate_entry(entry, 0)


def test_shell_generator_runs_a_command(tmp_path):
    (tmp_path / "world.yaml").write_text("wall: 1\n")
    entries = [{"shell": {"out": "files/scene", "inputs": ["world.yaml"],
                          "command": "cp {inputs[0]} {out}/scene.json"}}]
    records = run_input_generators(str(tmp_path), entries)
    assert records[0]["outputs"] == ["files/scene/scene.json"]


def test_shell_missing_declared_input_is_reported_before_running(tmp_path):
    entries = [{"shell": {"out": "files/scene", "inputs": ["absent.yaml"],
                          "command": "true"}}]
    with pytest.raises(CampaignConfigError, match="do not exist"):
        run_input_generators(str(tmp_path), entries)


def test_shell_keeps_a_manifest_the_command_wrote(tmp_path):
    """A tool that reports its own (transitive) inputs must not be downgraded to ours.

    The hand-listed ``inputs`` names only the entry point; a compiler that knows the world
    it was told about also inherits from another one says far more, and overwriting that
    would silently shrink the staleness check back to a single file.
    """
    (tmp_path / "world.yaml").write_text("wall: 1\n")
    (tmp_path / "parent.yaml").write_text("floor: 1\n")
    # A stand-in for a real compiler reporting its transitive sources (rst-export-web
    # --manifest). Written as a script rather than inline JSON because the command string
    # is str.format()-expanded, so literal braces would need doubling.
    (tmp_path / "compile.py").write_text(textwrap.dedent(f"""\
        import json, shutil, sys
        out = sys.argv[1]
        shutil.copy(sys.argv[2], out + "/scene.json")
        json.dump({{"inputs": [{str(tmp_path / 'world.yaml')!r},
                               {str(tmp_path / 'parent.yaml')!r}]}},
                  open(out + "/{MANIFEST_NAME}", "w"))
    """))
    entries = [{"shell": {
        "out": "files/scene", "inputs": ["world.yaml"],
        "command": f"{sys.executable} {tmp_path / 'compile.py'} {{out}} {{inputs[0]}}"}}]
    records = run_input_generators(str(tmp_path), entries)
    assert len(records[0]["inputs"]) == 2

    # ...and the extra input it reported really does drive regeneration.
    (tmp_path / "parent.yaml").write_text("floor: 2\n")
    assert run_input_generators(str(tmp_path), entries)[0]["cached"] is False


def test_shell_finds_a_tool_installed_beside_the_interpreter(tmp_path, monkeypatch):
    """A console script belongs to its environment, not to the launching shell's PATH.

    A service inherits the PATH of whatever started it, which the .vast author cannot see;
    resolving beside ``sys.executable`` first makes "the tool this environment provides"
    the answer instead.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python").write_text("")
    tool = fake_bin / "mytool"
    # No external commands: PATH is blanked below, and the runner already made "$1".
    tool.write_text("#!/bin/sh\necho made > \"$1/out.txt\"\n")
    tool.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(fake_bin / "python"))
    monkeypatch.setenv("PATH", "/nonexistent")

    entries = [{"shell": {"out": "files/thing", "command": "mytool {out}"}}]
    records = run_input_generators(str(tmp_path), entries)
    assert records[0]["outputs"] == ["files/thing/out.txt"]


def test_shell_reports_where_it_looked_for_a_missing_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    entries = [{"shell": {"out": "files/thing", "command": "absent-tool {out}"}}]
    with pytest.raises(CampaignConfigError) as excinfo:
        run_input_generators(str(tmp_path), entries)
    message = str(excinfo.value)
    assert "absent-tool" in message and "Looked in:" in message


def test_write_and_read_manifest_roundtrip(tmp_path):
    (tmp_path / "a").write_text("x")
    write_manifest(str(tmp_path), [str(tmp_path / "a"), str(tmp_path / "missing")])
    assert read_manifest(str(tmp_path)) == [str(tmp_path / "a")]


def test_read_manifest_returns_none_when_absent(tmp_path):
    assert read_manifest(str(tmp_path)) is None


def test_resolve_builtin_shell():
    assert resolve_input_generator("shell", "") is Shell
