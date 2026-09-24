# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a documentation search is allowed to cost.

A search reply is read by an LLM, where every line is context spent. The corpus is
large enough that a term as common as a product name matches thousands of lines, so an
unbounded reply is megabytes -- more than a client can carry, for a question the first
few excerpts and a page name already answer. These pin the bound and, as importantly,
that the reply says what it left out.
"""

import json
import types

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

# The upstream repositories a campaign runs on is documented in its own repository. Serving only
# robovast's pages meant an agent on the MCP path could not reach the world format, the
# plugin reference, or the scenario DSL at all -- and a search for them returned zero, which
# reads as "no such thing" rather than "not indexed here".


def _corpus_dir(tmp_path, name, pages):
    d = tmp_path / name / "docs"
    d.mkdir(parents=True)
    for stem, text in pages.items():
        (d / f"{stem}.rst").write_text(text, encoding="utf-8")
    return d


class _FakeCatalogClient:
    """The one call ``_upstream_pages`` makes: resolve the image, then exec in it."""

    def __init__(self, pages, exit_code=0):
        self._pages = pages
        self._exit_code = exit_code
        self.commands = []

    def resolve_image(self, request):
        return types.SimpleNamespace(image="ghcr.io/example/robovast-roqsim:2.1.0")

    def exec_in_container(self, request):
        self.commands.append((request.container, request.command))
        payload = json.dumps({"items": [{"name": n, "text": t}
                                        for n, t in self._pages.items()]})
        return types.SimpleNamespace(exit_code=self._exit_code, stdout=payload, stderr="")


def _with_image(monkeypatch, client):
    from robovast.mcp_server.plugins import image_catalog
    from robovast.service import image_catalog as service_catalog

    monkeypatch.setattr(image_catalog.service_access, "service_client", lambda: client)
    with service_catalog.CACHE_LOCK:
        service_catalog.LIST_CACHE.clear()


def test_the_image_answers_for_its_own_pages(tmp_path, monkeypatch):
    """The pages that answer a question about a world's format have to be the simulator's
    that campaign runs, and only the image knows which that is."""
    client = _FakeCatalogClient({"interfaces": "World YAML\n==========\n\ncomponents list\n"})
    _with_image(monkeypatch, client)

    pages, error = docs._upstream_pages("/sources/ws-1/w.vast")

    assert not error
    assert "roqsim-interfaces" in pages, "served under the corpus's own prefix"
    assert pages["roqsim-interfaces"][0] == "World YAML"
    container, command = client.commands[0]
    assert container == "simulation", "roqsim lives in the simulator's image"
    assert "/opt/roqsim/docs" in command, "the source tree the image already carries"


def test_a_search_without_an_address_stays_cheap_and_says_where_else_to_look(monkeypatch):
    """Zero results reads as "no such thing". These are RoboVAST's pages only, so a miss
    names the argument that reaches the simulator's rather than leaving the caller to guess."""
    def _never(*_a, **_k):
        raise AssertionError("an address-less search reached for an image")

    monkeypatch.setattr(docs, "_upstream_pages", _never)

    out = docs.search_docs(query="zzz-no-such-term-zzz")

    assert out["total"] == 0
    assert "address=" in out["note"]


def test_a_page_from_the_image_is_read_by_its_prefixed_name(monkeypatch):
    client = _FakeCatalogClient({"plugins": "Plugins\n=======\n\nkeys\n"})
    _with_image(monkeypatch, client)

    out = docs.search_docs(page="roqsim-plugins", address="/sources/ws-1/w.vast")

    assert out["title"] == "Plugins"
    assert "keys" in out["content"]


def test_an_image_that_cannot_answer_is_reported_not_guessed_at(monkeypatch):
    client = _FakeCatalogClient({}, exit_code=1)
    _with_image(monkeypatch, client)

    out = docs.search_docs(query="anything", address="/sources/ws-1/w.vast")

    assert "error" in out


def test_a_page_name_both_repositories_use_does_not_shadow(tmp_path):
    """Both carry an `architecture` page. Letting one win answers a question about the
    upstream with robovast's own page, which is worse than not answering it."""
    extra = _corpus_dir(tmp_path, "upstream", {"architecture": "A\n=\n\nupstream\n"})
    loaded = docs._load_corpus(extra, prefix="upstream")
    assert set(loaded) == {"upstream-architecture"}


def test_an_extra_corpus_is_read_from_the_environment(tmp_path, monkeypatch):
    extra = _corpus_dir(tmp_path, "upstream", {"plugins": "P\n=\n\nkeys\n"})
    monkeypatch.setenv(docs.DOCS_EXTRA_ENV, f"upstream={extra}")
    assert docs._env_doc_roots() == [("upstream", extra)]


def test_a_bare_path_takes_its_label_from_the_directory_it_is_in(tmp_path, monkeypatch):
    extra = _corpus_dir(tmp_path, "upstream", {"plugins": "P\n=\n\nkeys\n"})
    monkeypatch.setenv(docs.DOCS_EXTRA_ENV, str(extra))
    assert docs._env_doc_roots() == [("upstream", extra)]


def test_a_corpus_that_is_not_there_is_reported_not_guessed_at(tmp_path, monkeypatch, caplog):
    """Set-but-wrong is a misconfiguration, as it is for ROBOVAST_DOCS_DIR."""
    monkeypatch.setenv(docs.DOCS_EXTRA_ENV, f"upstream={tmp_path / 'nope'}")
    with caplog.at_level("WARNING"):
        assert docs._env_doc_roots() == []
    assert "not a directory" in caplog.text
