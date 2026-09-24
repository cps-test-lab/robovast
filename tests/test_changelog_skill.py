# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The changelog skill's tool: what it collects from a range of merges, what its check
refuses, and what it hands a release.

It ships in the plugin rather than as loose glue, so these tests load it by path and run
it against a repository built in a temporary directory.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "skills" / "changelog" / "changelog.py"


def _load():
    spec = importlib.util.spec_from_file_location("changelog", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve deferred annotations there
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo: Path, path: str, subject: str, body: str = "") -> str:
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(subject + "\n")
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", subject, *(["-m", body] if body else []))
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """A release tag, then three merges: two squash commits with a number and one direct
    push without, each touching a different part of the tree."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, "README.md", "Start")
    _git(tmp_path, "tag", "v0.1.0")
    _commit(tmp_path, "src/robovast/service/app.py", "Serve the thing (#12)",
            "One archive serves all directions.\n\nHow it is threaded through.")
    _commit(tmp_path, "src/robovast_nav/mcp_plugin.py", "Retire the plugin (#13)")
    sha = _commit(tmp_path, "docs/index.rst", "Fix a typo")
    return tmp_path, sha


def _run(tool, capsys, *argv):
    code = tool.main(list(argv))
    return code, capsys.readouterr().out


def test_collect_keys_each_merge_and_names_its_areas(repo, capsys):
    tool = _load()
    root, sha = repo
    code, out = _run(tool, capsys, "--root", str(root), "collect", "--head", "HEAD")
    assert code == 0
    assert "v0.1.0..HEAD: 3 merges" in out
    assert "#12  Serve the thing" in out
    assert "areas: src/robovast/service" in out
    assert "One archive serves all directions." in out, "the first paragraph, not the rest"
    assert "How it is threaded" not in out
    assert "areas: src/robovast_nav" in out
    assert f"{sha[:8]}  Fix a typo" in out, "a direct push is keyed by its short sha"


def test_the_summary_is_one_paragraph_not_the_whole_description():
    tool = _load()
    assert tool.SUMMARY_CHARS < 1000


def _changelog(root: Path, section: str) -> None:
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## 0.2.0\n\n" + section + "\n")


def _check(tool, capsys, root):
    return _run(tool, capsys, "--root", str(root), "check", "--version", "0.2.0",
                "--head", "HEAD")


def test_check_passes_a_short_flat_list(repo, capsys):
    """A user reads the few changes they must know about; which merge made each is optional,
    since git holds the rest."""
    tool = _load()
    root, _ = repo
    _changelog(root, "- **Plugin removed** — its reads are answered by core (#13)\n"
                     "- **Archives** — a workspace travels as one, and a long line\n"
                     "  continues indented\n"
                     "- **Docs** — spelled consistently\n")
    code, out = _check(tool, capsys, root)
    assert code == 0 and out.startswith("ok"), out
    assert "3 entries, 3 merges since v0.1.0" in out


def test_check_refuses_more_than_the_top_changes(repo, capsys):
    tool = _load()
    root, _ = repo
    _changelog(root, "".join(f"- **Topic {i}** — changed\n" for i in range(tool.MAX_ENTRIES + 1)))
    code, out = _check(tool, capsys, root)
    assert code == 1 and f"at most {tool.MAX_ENTRIES}" in out


def test_check_refuses_headings_prose_and_an_entry_without_a_topic(repo, capsys):
    """An Internal section is the shape this refuses: the section is for users."""
    tool = _load()
    root, _ = repo
    _changelog(root, "### Added\n\n- **Archives** — a workspace travels as one\n"
                     "- no topic here\n\n### Internal\n\nMerges a user does not see: #12\n")
    code, out = _check(tool, capsys, root)
    assert code == 1
    assert "not an entry" in out and "### Internal" in out
    assert "not '- **Topic** — what changed': - no topic here" in out


def test_check_refuses_a_long_entry_but_not_its_citations(repo, capsys):
    tool = _load()
    root, _ = repo
    fits = "x" * (tool.MAX_ENTRY_CHARS - len("- **T** — "))
    _changelog(root, f"- **T** — {fits} (#12, #13)\n")
    assert _check(tool, capsys, root)[0] == 0, "citations do not count toward the length"
    _changelog(root, f"- **T** — {fits}x (#12)\n")
    code, out = _check(tool, capsys, root)
    assert code == 1 and f"at most {tool.MAX_ENTRY_CHARS}" in out


def test_check_refuses_a_number_that_is_no_merge_in_the_range(repo, capsys):
    tool = _load()
    root, _ = repo
    _changelog(root, "- **Thing** — served (#12)\n- **Other** — something (#99)\n")
    code, out = _check(tool, capsys, root)
    assert code == 1 and "cited but not merged" in out and "#99" in out


def test_check_refuses_an_empty_section(repo, capsys):
    tool = _load()
    root, _ = repo
    _changelog(root, "")
    code, out = _check(tool, capsys, root)
    assert code == 1 and "no entries" in out


def test_check_refuses_a_missing_file_or_section(repo, capsys):
    tool = _load()
    root, _ = repo
    code, out = _run(tool, capsys, "--root", str(root), "check", "--version", "0.2.0",
                     "--head", "HEAD")
    assert code == 1 and "does not exist" in out
    _changelog(root, "- x (#12) (#13)")
    code, out = _run(tool, capsys, "--root", str(root), "check", "--version", "0.3.0",
                     "--head", "HEAD")
    assert code == 1 and "no '## 0.3.0' section" in out


def test_section_is_the_body_under_its_heading_only(repo, capsys):
    tool = _load()
    root, _ = repo
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.2.0\n\n- new (#12)\n\n## 0.1.0\n\n- old (#1)\n")
    code, out = _run(tool, capsys, "--root", str(root), "section", "--version", "0.2.0")
    assert code == 0
    assert out.strip() == "- new (#12)"


def test_the_range_defaults_to_the_newest_release_tag(repo, capsys):
    tool = _load()
    root, _ = repo
    _git(root, "tag", "v0.1.1", "HEAD~1")
    code, out = _run(tool, capsys, "--root", str(root), "collect", "--head", "HEAD")
    assert code == 0 and out.startswith("v0.1.1..HEAD: 1 merges")
