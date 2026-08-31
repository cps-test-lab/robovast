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
