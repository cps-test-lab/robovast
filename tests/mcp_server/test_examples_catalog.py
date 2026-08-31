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
    assert "not present in this checkout" in files["gone.txt"]["note"]
    assert "content" not in files["gone.txt"]
    # The file that is there is unaffected.
    assert files["demo.vast"]["content"] == "description: a demo"
