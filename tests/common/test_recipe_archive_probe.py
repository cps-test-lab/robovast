# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What the archive probe concludes from what the snapshot host answered.

`check_recipe` exists to notice that a dated apt archive has been pruned, because a campaign
recorded against a pruned archive can no longer be rebuilt from its recipe. That finding is
worth failing a build over. "The mirror returned 502" is not that finding -- it is a fact about
the host -- and conflating the two fails pull requests during someone else's outage while
telling nobody anything about the archive.

So these pin the split: which answers are definitive, which are retried, and which of the three
verdicts costs an exit code.
"""

import sys
from pathlib import Path

# `tools/` is scripts, not an installed package -- the sibling checkers are reached the same
# way. The import has to follow the path insertion, hence the disables.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

# pylint: disable=wrong-import-position
import check_recipe  # noqa: E402


def _answers(monkeypatch, *statuses):
    """Serve *statuses* one per probe, and record how many probes were made."""
    seen = []

    def probe(url):
        seen.append(url)
        status = statuses[min(len(seen) - 1, len(statuses) - 1)]
        return status, (f"HTTP {status}" if status else "connection refused")

    monkeypatch.setattr(check_recipe, "_probe", probe)
    monkeypatch.setattr(check_recipe, "_BACKOFF_S", 0.0)
    return seen


def test_a_served_archive_is_asked_once(monkeypatch):
    """The common case pays for no retries."""
    seen = _answers(monkeypatch, 200)
    assert check_recipe._serves("http://example.com/Release")[0] == "serves"
    assert len(seen) == 1


def test_a_pruned_archive_is_gone_on_the_first_answer(monkeypatch):
    """404 is the archive answering, and the answer is no -- retrying it only wastes the wait."""
    seen = _answers(monkeypatch, 404)
    verdict, why = check_recipe._serves("http://example.com/Release")
    assert (verdict, why) == ("gone", "HTTP 404")
    assert len(seen) == 1


def test_a_gateway_error_is_retried_and_a_recovery_counts(monkeypatch):
    """The flake this exists for: one bad minute on the mirror, then the same URL serves."""
    seen = _answers(monkeypatch, 502, 502, 200)
    assert check_recipe._serves("http://example.com/Release")[0] == "serves"
    assert len(seen) == 3


def test_a_gateway_error_that_never_clears_is_unverified_not_gone(monkeypatch):
    """Because it is not evidence about the archive, and the check must not claim it is."""
    seen = _answers(monkeypatch, 502)
    verdict, why = check_recipe._serves("http://example.com/Release")
    assert verdict == "unverified"
    assert "HTTP 502" in why and "attempts" in why
    assert len(seen) == check_recipe._ATTEMPTS


def test_never_reaching_the_host_at_all_is_unverified_too(monkeypatch):
    """A refused connection or a timeout says as little about the archive as a 502 does."""
    _answers(monkeypatch, 0)
    verdict, why = check_recipe._serves("http://example.com/Release")
    assert verdict == "unverified"
    assert "connection refused" in why


def test_a_rate_limit_is_retried_despite_being_a_4xx(monkeypatch):
    """429 is the host declining to answer yet, not the archive being gone."""
    seen = _answers(monkeypatch, 429, 200)
    assert check_recipe._serves("http://example.com/Release")[0] == "serves"
    assert len(seen) == 2


def test_a_403_is_definitive(monkeypatch):
    """An archive we may not read is a rebuild we cannot do; waiting does not change it."""
    seen = _answers(monkeypatch, 403)
    assert check_recipe._serves("http://example.com/Release")[0] == "gone"
    assert len(seen) == 1


DOCKERFILE = """\
ARG ROS_DISTRO=jazzy
ARG ROS_BASE_DIGEST=sha256:{d}
ARG UBUNTU_SNAPSHOT=20260819T003043Z
ARG ROS_SNAPSHOT=2026-06-18
""".format(d="1" * 64)


def _run(monkeypatch, capsys, tmp_path, *statuses):
    path = tmp_path / "Dockerfile"
    path.write_text(DOCKERFILE, encoding="utf-8")
    _answers(monkeypatch, *statuses)
    monkeypatch.setattr(sys, "argv", ["check_recipe.py", "--dockerfile", str(path)])
    return check_recipe.main(), capsys.readouterr().out


def test_an_unreachable_archive_does_not_fail_the_check(monkeypatch, capsys, tmp_path):
    """The whole point: a complete recipe plus an unreachable mirror is not a broken recipe.
    It is reported as a warning, so the run still says which archive went unchecked."""
    code, out = _run(monkeypatch, capsys, tmp_path, 502)
    assert code == 0
    assert "::warning::" in out and "::error::" not in out
    assert "unverified" in out


def test_a_pruned_archive_still_fails_the_check(monkeypatch, capsys, tmp_path):
    """The finding this check was built for survives the retry logic in front of it."""
    code, out = _run(monkeypatch, capsys, tmp_path, 404)
    assert code == 1
    assert "::error::" in out and "no longer serves" in out


def test_a_complete_recipe_whose_archives_serve_passes_silently(monkeypatch, capsys, tmp_path):
    code, out = _run(monkeypatch, capsys, tmp_path, 200)
    assert code == 0
    assert "::warning::" not in out and "::error::" not in out
