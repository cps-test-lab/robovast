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


def test_input_outside_the_project_is_advised_not_rejected(tmp_path, caplog):
    """Legitimate from the tree, broken from a workspace — say so without failing it.

    Only the project directory is copied into a service workspace, so a sibling-checkout
    input composes in place and vanishes on the cluster lane. That is a warning, because
    reading a sibling checkout on purpose is a real arrangement, not a mistake.
    """
    from robovast.common.config_validation import _generator_problems

    project = tmp_path / "proj"
    project.mkdir()
    (tmp_path / "outside.yaml").write_text("x: 1\n")
    raw = {"execution": {"generate": [
        {"shell": {"out": "files/scene", "inputs": ["../outside.yaml"],
                   "command": "true"}}]}}
    with caplog.at_level("WARNING"):
        problems = _generator_problems(raw, str(project))
    assert problems == []
    assert "outside the project directory" in caplog.text


def test_input_inside_the_project_is_not_advised(tmp_path, caplog):
    from robovast.common.config_validation import _generator_problems

    (tmp_path / "world.yaml").write_text("x: 1\n")
    raw = {"execution": {"generate": [
        {"shell": {"out": "files/scene", "inputs": ["world.yaml"], "command": "true"}}]}}
    with caplog.at_level("WARNING"):
        assert _generator_problems(raw, str(tmp_path)) == []
    assert "outside the project directory" not in caplog.text


def test_inputs_the_host_cannot_see_are_never_cached(tmp_path):
    """A containerized generator reports paths inside the container.

    None of them exist here, so a key built by hashing them reduces to the generator's own
    name and parameters — constant, and the first stamp would then satisfy every later
    composition however much the image changed. Unverifiable inputs mean regenerate.
    """
    (tmp_path / "gen.py").write_text(textwrap.dedent("""\
        import json, os
        from robovast.common.input_generation import BaseInputGenerator, MANIFEST_NAME

        class G(BaseInputGenerator):
            def __call__(self, vast_dir, out_dir, **kw):
                with open(os.path.join(out_dir, "scene.json"), "w") as fh:
                    fh.write("v1")
                with open(os.path.join(out_dir, MANIFEST_NAME), "w") as fh:
                    json.dump({"inputs": ["/usr/local/share/pkg/world.yaml"]}, fh)
                return True, "ok"
    """))
    entries = [{"./gen.py:G": {"out": "files/scene"}}]
    run_input_generators(str(tmp_path), entries)
    assert run_input_generators(str(tmp_path), entries)[0]["cached"] is False


# -- a staged tree at a fixed container path ------------------------------------------


class _FakeRunner:
    """A container runner that records what it was asked to expose instead of running it."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.exposed = {}

    def expose(self, host_path, container_path):
        self.exposed[container_path] = host_path


class _NoExposeRunner:
    def __init__(self, workspace):
        self.workspace = workspace


def test_a_staged_tree_can_be_asked_for_at_a_fixed_container_path(tmp_path):
    """Some inputs cannot be read from wherever they happen to land.

    A world names its meshes by the absolute path the job mounted them at, so it only
    compiles where that path resolves. ``mount_at`` is what lets the caller say so, and the
    in-container path handed back is the mount -- not the staging slot.
    """
    from robovast.common.input_generation import stage_for_container

    tree = tmp_path / "_config"
    (tree / "environments" / "hex").mkdir(parents=True)
    (tree / "environments" / "hex" / "hex.stl").write_bytes(b"solid\n")
    (tree / "world.yaml").write_text("mesh: /config/environments/hex/hex.stl\n")
    runner = _FakeRunner(str(tmp_path / "ws"))
    os.makedirs(runner.workspace, exist_ok=True)

    _out, staged = stage_for_container(runner, str(tmp_path / "out"), [str(tree)],
                                       mount_at={str(tree): "/config"})

    assert staged == ["/config"]
    host_copy = runner.exposed["/config"]
    # The tree's internal layout is what makes a sibling lookup (a mesh's json-ld) resolve.
    assert os.path.isfile(os.path.join(host_copy, "environments", "hex", "hex.stl"))
    assert os.path.isfile(os.path.join(host_copy, "world.yaml"))


def test_a_backend_that_cannot_mount_refuses_rather_than_running(tmp_path):
    """Running anyway would fail deep inside the tool on a missing file, which reads as the
    tool's own problem rather than as an execution backend that cannot do what was asked."""
    from robovast.common.input_generation import stage_for_container

    tree = tmp_path / "_config"
    tree.mkdir()
    (tree / "world.yaml").write_text("x: 1\n")
    runner = _NoExposeRunner(str(tmp_path / "ws"))
    os.makedirs(runner.workspace, exist_ok=True)

    with pytest.raises(CampaignConfigError, match="cannot expose a staged input"):
        stage_for_container(runner, str(tmp_path / "out"), [str(tree)],
                            mount_at={str(tree): "/config"})


def test_mount_at_naming_no_declared_input_is_a_loud_typo(tmp_path):
    (tmp_path / "world.yaml").write_text("x: 1\n")
    entries = [{"shell": {"out": "files/scene", "inputs": ["world.yaml"],
                          "mount_at": {"worlds.yaml": "/config"},
                          "command": "true", "image": "example/img:1"}}]
    with pytest.raises(CampaignConfigError, match="not one of its 'inputs'"):
        run_input_generators(str(tmp_path), entries)


def test_a_failed_command_carries_its_output_into_the_error(tmp_path):
    """`CalledProcessError` renders as "returned non-zero exit status 1" and nothing else.

    Without the tool's own message the failure reaches a user as a command line and no
    cause -- which is how a missing mesh presented itself as "no 3D geometry".
    """
    script = tmp_path / "boom.py"
    script.write_text(textwrap.dedent("""\
        import sys
        print("about to fail")
        sys.stderr.write("PluginError: 'mesh' file does not exist: /config/x.stl\\n")
        sys.exit(1)
    """))
    entries = [{"shell": {"out": "files/scene",
                          "command": f"{sys.executable} {script}"}}]
    with pytest.raises(CampaignConfigError) as err:
        run_input_generators(str(tmp_path), entries)
    assert "'mesh' file does not exist" in str(err.value)
