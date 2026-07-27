# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""One confinement check, shared by the workspace tree and the results tree.

The escapes below were each rejected by only *some* of the three implementations
this replaced, which is the reason to have one: ``get_job_log`` tested only that the
campaign dir was among the resolved parents, and ``get_run_file`` only that the
resolved string had the run dir as a prefix — neither refused a ``~`` path, and
neither considered a symlink planted inside the root.
"""

import pytest

from robovast.common.safe_path import UnsafePathError, safe_join


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    (r / "sub").mkdir(parents=True)
    (r / "sub" / "file.txt").write_text("ok")
    (tmp_path / "outside.txt").write_text("secret")
    return r


def test_resolves_a_path_inside_the_root(root):
    assert safe_join(root, "sub/file.txt") == (root / "sub" / "file.txt").resolve()


def test_the_root_itself_is_allowed(root):
    assert safe_join(root, ".") == root.resolve()


@pytest.mark.parametrize("bad", ["", "   ", "/etc/passwd", "~/secrets",
                                 "../outside.txt", "sub/../../outside.txt"])
def test_refuses_escapes(root, bad):
    with pytest.raises(UnsafePathError):
        safe_join(root, bad)


def test_refuses_a_symlink_pointing_out_of_the_root(root, tmp_path):
    """The check is on the RESOLVED path, so a link inside the root cannot redirect."""
    (root / "escape").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(UnsafePathError, match="escapes"):
        safe_join(root, "escape")


def test_unsafe_path_error_is_a_value_error(root):
    """Existing ``except ValueError`` handlers must keep mapping it to a 4xx."""
    assert issubclass(UnsafePathError, ValueError)


def test_a_nonexistent_but_contained_path_is_allowed(root):
    """Confinement is not existence: callers do their own is_file/is_dir check."""
    assert safe_join(root, "sub/not-yet.csv") == (root / "sub" / "not-yet.csv").resolve()
