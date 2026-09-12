# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a documentation search is allowed to cost.

A search reply is read by an LLM, where every line is context spent. The corpus is
large enough that a term as common as a product name matches thousands of lines, so an
unbounded reply is megabytes -- more than a client can carry, for a question the first
few excerpts and a page name already answer. These pin the bound and, as importantly,
that the reply says what it left out.
"""

import pytest

from robovast.mcp_server.plugins import docs


@pytest.fixture
def corpus(monkeypatch):
    """A two-page corpus: one page with clustered matches, one with scattered ones."""
    pages = {
        "clustered": "\n".join(["intro"] + ["needle here"] * 6 + ["outro"]),
        "scattered": "\n".join(
            f"needle {i}" if i % 10 == 0 else f"filler {i}" for i in range(60)),
        "quiet": "nothing to find here",
    }
    monkeypatch.setattr(docs, "_doc_files", {name: name for name in pages})
    monkeypatch.setattr(docs, "_doc_content", pages)
    monkeypatch.setattr(docs, "_doc_meta", {name: name.title() for name in pages})
    return pages


def test_a_page_with_no_match_is_absent(corpus):
    result = docs.search_docs(query="needle")
    assert {r["page"] for r in result["results"]} == {"clustered", "scattered"}


def test_adjacent_matches_share_one_excerpt(corpus):
    """Six consecutive matching lines are one place in the document, not six. Returning
    a five-line window per match repeats the same lines and makes a page's reply grow
    with how clustered its matches are."""
    page = next(r for r in docs.search_docs(query="needle")["results"]
                if r["page"] == "clustered")
    assert page["matching_lines"] == 6
    assert page["excerpts_total"] == 1
    assert page["matches"][0]["matching_lines"] == 6
    assert page["truncated"] is False


def test_excerpts_are_capped_per_page_and_the_cap_is_reported(corpus):
    page = next(r for r in docs.search_docs(query="needle", limit=2)["results"]
                if r["page"] == "scattered")
    assert len(page["matches"]) == 2
    assert page["matching_lines"] == 6
    assert page["excerpts_total"] == 6
    assert page["truncated"] is True
    assert docs.search_docs(query="needle", limit=2)["truncated"] is True


def test_limit_zero_returns_every_excerpt(corpus):
    page = next(r for r in docs.search_docs(query="needle", limit=0)["results"]
                if r["page"] == "scattered")
    assert len(page["matches"]) == page["excerpts_total"] == 6
    assert page["truncated"] is False


def test_the_totals_count_every_match_not_the_returned_ones(corpus):
    result = docs.search_docs(query="needle", limit=1)
    assert result["matching_lines_total"] == 12
    assert sum(len(r["matches"]) for r in result["results"]) == 2


def test_a_term_that_matches_everywhere_says_it_is_a_sample(corpus, monkeypatch):
    """Otherwise a sample of the pages a word appears in reads as what the docs say
    about it."""
    monkeypatch.setattr(docs, "_COMMON_TERM_LINES", 5)
    assert "sample" in docs.search_docs(query="needle")["note"]
    assert "note" not in docs.search_docs(query="intro")


def test_reading_one_page_is_unbounded(corpus):
    """The per-page cap is about a search; a caller that named a page wants the page."""
    assert docs.search_docs(page="clustered")["content"] == corpus["clustered"]


def test_an_unknown_page_names_the_ones_there_are(corpus):
    assert "clustered" in docs.search_docs(page="nope")["error"]


# -- Where the documentation is found -----------------------------------------
#
# The deployment that broke: robovast installed into site-packages, with the docs
# nowhere above the module. The walk cannot reach them, so an image has to say where
# they are -- and when it says it wrong, the reply has to distinguish that from a
# deployment that never pointed at them at all.


def test_an_env_pointing_at_a_real_directory_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOVAST_DOCS_DIR", str(tmp_path))
    assert docs._find_docs_dir() == tmp_path


def test_an_env_pointing_nowhere_serves_nothing_rather_than_other_docs(tmp_path, monkeypatch):
    """Falling through to the walk would serve whichever docs/ happens to sit above the
    module under a name the operator believes is theirs."""
    monkeypatch.setenv("ROBOVAST_DOCS_DIR", str(tmp_path / "absent"))
    assert docs._find_docs_dir() is None


def test_a_misconfigured_path_is_named_in_the_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOVAST_DOCS_DIR", str(tmp_path / "absent"))
    assert str(tmp_path / "absent") in docs._no_docs()["error"]


def test_an_unset_env_is_reported_as_unset(monkeypatch):
    monkeypatch.delenv("ROBOVAST_DOCS_DIR", raising=False)
    assert "not found" in docs._no_docs()["error"]


def test_a_source_checkout_needs_no_env(monkeypatch):
    """The walk that keeps `pip install -e .` working."""
    monkeypatch.delenv("ROBOVAST_DOCS_DIR", raising=False)
    found = docs._find_docs_dir()
    assert found is not None and any(found.glob("*.rst"))


# -- a corpus that reaches past this repository ----------------------------------

# The substrate a campaign runs on is documented in its own repository. Serving only
# robovast's pages meant an agent on the MCP path could not reach the world format, the
# plugin reference, or the scenario DSL at all -- and a search for them returned zero, which
# reads as "no such thing" rather than "not indexed here".


def _corpus_dir(tmp_path, name, pages):
    d = tmp_path / name / "docs"
    d.mkdir(parents=True)
    for stem, text in pages.items():
        (d / f"{stem}.rst").write_text(text, encoding="utf-8")
    return d


class _FakeEntryPoint:
    def __init__(self, name, target):
        self.name = name
        self._target = target

    def load(self):
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


def _with_entry_points(monkeypatch, eps):
    import importlib.metadata as md
    monkeypatch.setattr(md, "entry_points", lambda group=None: list(eps))


def test_a_package_publishing_docs_has_them_served_under_its_own_prefix(tmp_path, monkeypatch):
    extra = _corpus_dir(tmp_path, "substrate", {
        "interfaces": "World YAML\n==========\n\nThe components list.\n"})

    class _Pkg:
        DOCS_DIR = str(extra)

    _with_entry_points(monkeypatch, [_FakeEntryPoint("substrate", _Pkg)])
    assert docs._entry_point_doc_roots() == [("substrate", extra)]

    label, root = docs._entry_point_doc_roots()[0]
    loaded = docs._load_corpus(root, prefix=label)
    assert "substrate-interfaces" in loaded
    assert loaded["substrate-interfaces"][2] == "substrate"


def test_a_package_that_exposes_no_docs_dir_is_skipped_with_a_reason(monkeypatch, caplog):
    class _Pkg:
        pass

    _with_entry_points(monkeypatch, [_FakeEntryPoint("substrate", _Pkg)])
    with caplog.at_level("WARNING"):
        assert docs._entry_point_doc_roots() == []
    assert "DOCS_DIR" in caplog.text


def test_a_page_name_both_repositories_use_does_not_shadow(tmp_path):
    """Both carry an `architecture` page. Letting one win answers a question about the
    substrate with robovast's own page, which is worse than not answering it."""
    extra = _corpus_dir(tmp_path, "substrate", {"architecture": "A\n=\n\nsubstrate\n"})
    loaded = docs._load_corpus(extra, prefix="substrate")
    assert set(loaded) == {"substrate-architecture"}


def test_an_extra_corpus_is_read_from_the_environment(tmp_path, monkeypatch):
    extra = _corpus_dir(tmp_path, "substrate", {"plugins": "P\n=\n\nkeys\n"})
    monkeypatch.setenv(docs.DOCS_EXTRA_ENV, f"substrate={extra}")
    assert docs._env_doc_roots() == [("substrate", extra)]


def test_a_bare_path_takes_its_label_from_the_directory_it_is_in(tmp_path, monkeypatch):
    extra = _corpus_dir(tmp_path, "substrate", {"plugins": "P\n=\n\nkeys\n"})
    monkeypatch.setenv(docs.DOCS_EXTRA_ENV, str(extra))
    assert docs._env_doc_roots() == [("substrate", extra)]


def test_a_corpus_that_is_not_there_is_reported_not_guessed_at(tmp_path, monkeypatch, caplog):
    """Set-but-wrong is a misconfiguration, as it is for ROBOVAST_DOCS_DIR."""
    monkeypatch.setenv(docs.DOCS_EXTRA_ENV, f"substrate={tmp_path / 'nope'}")
    with caplog.at_level("WARNING"):
        assert docs._env_doc_roots() == []
    assert "not a directory" in caplog.text


def test_one_packages_broken_entry_point_does_not_cost_the_others_their_docs(
        tmp_path, monkeypatch):
    """A package that fails to import must not take every other package's docs with it."""
    extra = _corpus_dir(tmp_path, "substrate", {"plugins": "P\n=\n\nkeys\n"})

    class _Pkg:
        DOCS_DIR = str(extra)

    _with_entry_points(monkeypatch, [
        _FakeEntryPoint("broken", ImportError("no such module")),
        _FakeEntryPoint("substrate", _Pkg),
    ])
    assert docs._entry_point_doc_roots() == [("substrate", extra)]
