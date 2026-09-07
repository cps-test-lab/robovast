# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What the example catalog says about a file it cannot read.

The catalog is the git index, which lists a file whether or not the checkout still holds
it. Every reason a file has no content is a different fact, and the caller acts on each
differently -- fetch the bytes, or stop looking for a file that is not there.
"""

import subprocess

import pytest

from robovast.mcp_server.plugins import examples


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A git checkout holding one example, with one tracked file deleted from disk."""
    example = tmp_path / "configs" / "examples" / "demo"
    example.mkdir(parents=True)
    (example / "demo.vast").write_text("description: a demo\n")
    (example / "gone.txt").write_text("bye\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=a@example.com", "-c", "user.name=a",
                    "commit", "-qm", "example"], cwd=tmp_path, check=True)
    (example / "gone.txt").unlink()
    monkeypatch.setenv("ROBOVAST_EXAMPLES_DIR", str(tmp_path / "configs" / "examples"))
    examples._load_examples.cache_clear()  # pylint: disable=protected-access
    yield tmp_path
    examples._load_examples.cache_clear()  # pylint: disable=protected-access


def test_a_tracked_file_missing_from_the_checkout_says_so(checkout):
    """`is_binary` answers "binary" for anything it cannot open, so an absent file was
    reported as a binary asset — sending the caller to fetch bytes that do not exist."""
    files = {f["path"]: f for f in examples.get_example("demo")["files"]}
    assert "Binary" not in files["gone.txt"]["note"]
    assert "not present here" in files["gone.txt"]["note"]
    assert "content" not in files["gone.txt"]
    # The file that is there is unaffected.
    assert files["demo.vast"]["content"] == "description: a demo"


# -- Where the authored-file list comes from -----------------------------------
#
# The deployment that broke: an image carries the examples but not `.git`, so the tool
# that asks git which files are authored got no answer and served an empty catalog --
# reported as "no examples found", which reads like a directory that was never pointed at.


@pytest.fixture
def image(tmp_path, monkeypatch):
    """An image-shaped tree: the example files and a MANIFEST, and no git checkout.

    A generated file sits beside the authored ones, as it does in a real build: whether
    it is exposed is the whole point of the list.
    """
    example = tmp_path / "configs" / "examples" / "demo"
    (example / ".cache").mkdir(parents=True)
    (example / "demo.vast").write_text("description: a demo\n")
    (example / ".cache" / "generated.json").write_text("{}\n")
    (tmp_path / "configs" / "examples" / "MANIFEST").write_text(
        "# Authored files.\ndemo/demo.vast\n")
    monkeypatch.setenv("ROBOVAST_EXAMPLES_DIR", str(tmp_path / "configs" / "examples"))
    examples._load_examples.cache_clear()  # pylint: disable=protected-access
    yield tmp_path
    examples._load_examples.cache_clear()  # pylint: disable=protected-access


def test_without_a_checkout_the_manifest_is_the_catalog(image):
    listing = examples.get_example()
    assert [e["name"] for e in listing["examples"]] == ["demo"]
    assert examples.get_example("demo")["files"] == [
        {"path": "demo.vast", "content": "description: a demo"}]


def test_a_generated_file_beside_an_authored_one_stays_out(image):
    """The reason the list exists at all: both sit in the same directory, and only the
    manifest separates them once git is gone."""
    assert examples.get_example("demo")["files"] == [
        {"path": "demo.vast", "content": "description: a demo"}]


def test_the_manifest_is_not_itself_an_example(image):
    assert "MANIFEST" not in {e["name"] for e in examples.get_example()["examples"]}


def test_git_wins_over_a_stale_manifest(checkout):
    """In a checkout the index is current by construction; a manifest that had gone
    stale must not hide an example someone just added."""
    (checkout / "configs" / "examples" / "MANIFEST").write_text("nothing/at_all.vast\n")
    examples._load_examples.cache_clear()  # pylint: disable=protected-access
    assert [e["name"] for e in examples.get_example()["examples"]] == ["demo"]


def test_neither_source_says_which_deployment_it_is(tmp_path, monkeypatch):
    """Not a checkout and no manifest is a build that copied the examples in and left
    the list behind — a different fix from an unset variable, so a different sentence."""
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "demo.vast").write_text("description: a demo\n")
    monkeypatch.setenv("ROBOVAST_EXAMPLES_DIR", str(tmp_path))
    examples._load_examples.cache_clear()  # pylint: disable=protected-access
    error = examples.get_example()["error"]
    assert "MANIFEST" in error and "authored" in error
    examples._load_examples.cache_clear()  # pylint: disable=protected-access


def test_an_examples_dir_pointing_nowhere_is_named(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOVAST_EXAMPLES_DIR", str(tmp_path / "absent"))
    examples._load_examples.cache_clear()  # pylint: disable=protected-access
    assert str(tmp_path / "absent") in examples.get_example()["error"]
    examples._load_examples.cache_clear()  # pylint: disable=protected-access


def test_the_committed_manifest_matches_the_git_index():
    """The drift guard, as a test as well as a make target: the manifest is a snapshot,
    and a snapshot nobody compares is a snapshot that has already drifted."""
    manifest_tool = _load_manifest_tool()
    assert manifest_tool.manifest() == manifest_tool._MANIFEST.read_text(encoding="utf-8")


def _load_manifest_tool():
    """Import ``tools/examples_manifest.py``, which is a script rather than a module."""
    import importlib.util  # pylint: disable=import-outside-toplevel
    import pathlib  # pylint: disable=import-outside-toplevel
    path = pathlib.Path(__file__).resolve().parents[2] / "tools" / "examples_manifest.py"
    spec = importlib.util.spec_from_file_location("examples_manifest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
