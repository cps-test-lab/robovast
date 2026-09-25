"""The query bounds an operator states reach the engine, and a wrong one is refused by name."""

import pytest

from robovast.common.query_limits import MEMORY_ENV, THREADS_ENV, query_limits


def test_unset_means_the_engine_keeps_its_defaults(monkeypatch):
    monkeypatch.delenv(MEMORY_ENV, raising=False)
    monkeypatch.delenv(THREADS_ENV, raising=False)
    assert query_limits() == {}


@pytest.mark.parametrize("memory", ["4GB", "512MiB", " 1.5 GB ", "100000 bytes"])
def test_a_memory_limit_is_passed_as_duckdb_reads_it(monkeypatch, memory):
    monkeypatch.setenv(MEMORY_ENV, memory)
    monkeypatch.delenv(THREADS_ENV, raising=False)
    assert query_limits() == {"memory_limit": memory.strip()}


def test_threads_are_an_integer(monkeypatch):
    monkeypatch.delenv(MEMORY_ENV, raising=False)
    monkeypatch.setenv(THREADS_ENV, "2")
    assert query_limits() == {"threads": 2}


@pytest.mark.parametrize("value", ["lots", "4", "4 GBs", "-1GB"])
def test_a_memory_limit_duckdb_would_refuse_is_refused_by_name(monkeypatch, value):
    monkeypatch.setenv(MEMORY_ENV, value)
    with pytest.raises(ValueError, match=MEMORY_ENV):
        query_limits()


@pytest.mark.parametrize("value", ["0", "-2", "two", "1.5"])
def test_threads_below_one_or_not_a_number_are_refused_by_name(monkeypatch, value):
    monkeypatch.delenv(MEMORY_ENV, raising=False)
    monkeypatch.setenv(THREADS_ENV, value)
    with pytest.raises(ValueError, match=THREADS_ENV):
        query_limits()
