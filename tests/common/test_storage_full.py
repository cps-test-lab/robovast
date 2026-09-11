# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Which failures mean "the storage behind a write is full".

The predicate is what every surface asks before it reports a failure, so what it recognises
decides whether a caller is told "free space and retry" or "your request is wrong".
"""

import errno

from robovast.common.errors import is_storage_full


class _DiskFull(Exception):
    """What psycopg raises when Postgres's volume is full, by the attribute that says so."""

    sqlstate = "53100"


def test_a_write_with_no_space_left_is_storage_full():
    assert is_storage_full(OSError(errno.ENOSPC, "No space left on device"))


def test_an_exhausted_quota_is_storage_full():
    """The same fact on a filesystem with quotas: the write failed for lack of room."""
    assert is_storage_full(OSError(errno.EDQUOT, "Disk quota exceeded"))


def test_a_full_index_disk_is_storage_full():
    assert is_storage_full(_DiskFull("could not extend file"))


def test_other_io_failures_are_not():
    assert not is_storage_full(OSError(errno.EACCES, "Permission denied"))
    assert not is_storage_full(FileNotFoundError("gone"))
    assert not is_storage_full(ValueError("bad input"))


def test_a_translated_failure_is_still_recognised_by_its_cause():
    """A layer that turns the OSError into its own refusal must not hide what happened.

    Without following the cause, "the disk is full" reaches the caller as a 400 about their
    archive, and they go looking for a problem in a file that was fine.
    """
    try:
        try:
            raise OSError(errno.ENOSPC, "No space left on device")
        except OSError as exc:
            raise ValueError("could not extract the archive") from exc
    except ValueError as translated:
        assert is_storage_full(translated)


def test_a_failure_raised_while_handling_one_is_recognised_too():
    try:
        try:
            raise OSError(errno.ENOSPC, "No space left on device")
        except OSError:
            # Implicitly chained on purpose: the context, not a cause, is what is under test.
            raise RuntimeError(  # noqa: B904  # pylint: disable=raise-missing-from
                "cleanup after the failed write failed")
    except RuntimeError as during:
        assert is_storage_full(during)


def test_a_context_declared_irrelevant_is_not_followed():
    """``raise ... from None`` says the earlier exception is not the reason; believe it."""
    try:
        try:
            raise OSError(errno.ENOSPC, "No space left on device")
        except OSError:
            raise KeyError("no such campaign") from None
    except KeyError as unrelated:
        assert not is_storage_full(unrelated)
