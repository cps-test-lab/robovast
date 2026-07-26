# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The pure image-build core: the cache key (``build_hash``) and the rendered Dockerfile.

Both are shared verbatim by the local docker path and the in-cluster BuildKit path, so
what is asserted here holds on every backend. The properties under test are the two the
caching depends on: a hash that changes exactly when the built image would differ, and a
Dockerfile whose layer order lets an unchanged entry be reused.
"""

import zipfile

import pytest

from robovast.common.build_context import render_dockerignore
from robovast.service.image_build import (BuildSpec, build_hash,
                                          generate_dockerfile)

BASE = "ghcr.io/x/robovast:latest"


def _wheel(path, files, date_time=(2026, 1, 1, 0, 0, 0),
           compression=zipfile.ZIP_DEFLATED):
    """Write a wheel-shaped zip with controllable metadata."""
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for name, content in files.items():
            zf.writestr(zipfile.ZipInfo(name, date_time=date_time), content)
    return path


# ---------------------------------------------------------------------------
# build_hash — the cache key
# ---------------------------------------------------------------------------

def test_wheel_hash_ignores_zip_metadata(tmp_path):
    """Rebuilding a wheel from unchanged sources must not invalidate the image.

    pip stamps each member with its source file's mtime, so a branch switch or a fresh
    clone rewrites every timestamp and changes the wheel bytes while installing exactly
    the same files. Hashing raw bytes made that a full rebuild.
    """
    files = {"pkg/__init__.py": "x = 1\n", "pkg-1.0.dist-info/METADATA": "Name: pkg\n"}
    spec = BuildSpec(tag="t", python_packages=["pkg-1.0-py3-none-any.whl"])

    _wheel(tmp_path / "pkg-1.0-py3-none-any.whl", files)
    first = build_hash(spec, tmp_path, BASE)

    # Same content, different timestamps *and* compression → different bytes.
    _wheel(tmp_path / "pkg-1.0-py3-none-any.whl", files,
           date_time=(2027, 6, 30, 12, 30, 0), compression=zipfile.ZIP_STORED)
    assert build_hash(spec, tmp_path, BASE) == first


def test_wheel_hash_follows_real_content_change(tmp_path):
    spec = BuildSpec(tag="t", python_packages=["pkg-1.0-py3-none-any.whl"])
    _wheel(tmp_path / "pkg-1.0-py3-none-any.whl", {"pkg/__init__.py": "x = 1\n"})
    first = build_hash(spec, tmp_path, BASE)
    _wheel(tmp_path / "pkg-1.0-py3-none-any.whl", {"pkg/__init__.py": "x = 2\n"})
    assert build_hash(spec, tmp_path, BASE) != first


def test_wheel_hash_follows_renamed_member(tmp_path):
    """Same bytes-per-file but a different layout is a different install."""
    spec = BuildSpec(tag="t", python_packages=["pkg-1.0-py3-none-any.whl"])
    _wheel(tmp_path / "pkg-1.0-py3-none-any.whl", {"pkg/a.py": "x = 1\n"})
    first = build_hash(spec, tmp_path, BASE)
    _wheel(tmp_path / "pkg-1.0-py3-none-any.whl", {"pkg/b.py": "x = 1\n"})
    assert build_hash(spec, tmp_path, BASE) != first


def test_unreadable_wheel_does_not_collide(tmp_path):
    """A non-zip .whl falls back to raw bytes, not to hashing nothing."""
    spec = BuildSpec(tag="t", python_packages=["pkg-1.0-py3-none-any.whl"])
    (tmp_path / "pkg-1.0-py3-none-any.whl").write_bytes(b"not a zip at all")
    first = build_hash(spec, tmp_path, BASE)
    (tmp_path / "pkg-1.0-py3-none-any.whl").write_bytes(b"also not a zip")
    assert build_hash(spec, tmp_path, BASE) != first


def test_apt_order_does_not_affect_hash(tmp_path):
    a = BuildSpec(tag="t", system_packages=["libegl1", "libgomp1"])
    b = BuildSpec(tag="t", system_packages=["libgomp1", "libegl1"])
    assert build_hash(a, tmp_path, BASE) == build_hash(b, tmp_path, BASE)


def test_python_package_order_affects_hash(tmp_path):
    """Install order is semantic (deps must already be present) → part of the key."""
    a = BuildSpec(tag="t", python_packages=["rst", "rst_assets"])
    b = BuildSpec(tag="t", python_packages=["rst_assets", "rst"])
    assert build_hash(a, tmp_path, BASE) != build_hash(b, tmp_path, BASE)


def test_source_dir_content_affects_hash(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    spec = BuildSpec(tag="t", python_packages=["pkg"])
    first = build_hash(spec, tmp_path, BASE)
    (tmp_path / "pkg" / "mod.py").write_text("x = 2\n")
    assert build_hash(spec, tmp_path, BASE) != first


def test_ignored_dirs_do_not_affect_hash(tmp_path):
    """Results and caches are not build inputs — they must not force a rebuild."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    spec = BuildSpec(tag="t", python_packages=["pkg"])
    first = build_hash(spec, tmp_path, BASE)
    (tmp_path / "pkg" / "results").mkdir()
    (tmp_path / "pkg" / "results" / "run.db").write_text("noise")
    (tmp_path / "pkg" / "__pycache__").mkdir()
    (tmp_path / "pkg" / "__pycache__" / "mod.pyc").write_text("noise")
    assert build_hash(spec, tmp_path, BASE) == first


def test_base_image_affects_hash(tmp_path):
    spec = BuildSpec(tag="t", python_packages=["shapely>=2.0"])
    assert build_hash(spec, tmp_path, BASE) != build_hash(spec, tmp_path, "other:1")


# ---------------------------------------------------------------------------
# generate_dockerfile — the layer chain
# ---------------------------------------------------------------------------

def test_no_blanket_context_copy(tmp_path):
    """``COPY .`` would make any unrelated project file invalidate every pip layer."""
    (tmp_path / "wheels").mkdir()
    _wheel(tmp_path / "wheels" / "pkg-1.0-py3-none-any.whl", {"pkg/__init__.py": ""})
    spec = BuildSpec(tag="t",
                     python_packages=["wheels/pkg-1.0-py3-none-any.whl"])
    df = generate_dockerfile(spec, tmp_path, BASE)
    assert "COPY . " not in df
    assert ("COPY wheels/pkg-1.0-py3-none-any.whl "
            "/robovast_build_context/wheels/pkg-1.0-py3-none-any.whl") in df


def test_each_context_entry_copied_immediately_before_its_install(tmp_path):
    """A change to entry k must rebuild only k onward, so COPY/RUN must interleave."""
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "setup.py").write_text("")
    spec = BuildSpec(tag="t", python_packages=["a", "b"])
    lines = [ln for ln in generate_dockerfile(spec, tmp_path, BASE).splitlines()
             if ln.startswith(("COPY", "RUN --mount"))]
    assert lines[0].startswith("COPY a ")
    assert "-e /robovast_build_context/a" in lines[1]
    assert lines[2].startswith("COPY b ")
    assert "-e /robovast_build_context/b" in lines[3]


def test_pip_spec_entry_has_no_copy(tmp_path):
    spec = BuildSpec(tag="t", python_packages=["shapely>=2.0"])
    df = generate_dockerfile(spec, tmp_path, BASE)
    assert "COPY" not in df
    assert "'shapely>=2.0'" in df


def test_apt_packages_sorted_to_match_hash(tmp_path):
    """A YAML reorder keeps the hash, so it must keep the Dockerfile too."""
    a = generate_dockerfile(
        BuildSpec(tag="t", system_packages=["libgomp1", "libegl1"]), tmp_path, BASE)
    b = generate_dockerfile(
        BuildSpec(tag="t", system_packages=["libegl1", "libgomp1"]), tmp_path, BASE)
    assert a == b
    assert "libegl1 libgomp1" in a


def test_syntax_directive_is_first_line(tmp_path):
    """The cache mount below needs the pinned frontend, and it must lead the file."""
    df = generate_dockerfile(BuildSpec(tag="t"), tmp_path, BASE)
    assert df.splitlines()[0] == "# syntax=docker/dockerfile:1"


def test_pip_uses_a_cache_mount_not_no_cache_dir(tmp_path):
    spec = BuildSpec(tag="t", python_packages=["shapely>=2.0"])
    df = generate_dockerfile(spec, tmp_path, BASE)
    assert "--mount=type=cache,target=/root/.cache/pip" in df
    assert "--no-cache-dir" not in df


def test_privilege_bracket_is_preserved(tmp_path):
    """Build steps run as root; the image must still end unprivileged."""
    spec = BuildSpec(tag="t", python_packages=["shapely>=2.0"])
    lines = generate_dockerfile(spec, tmp_path, BASE).splitlines()
    assert lines[1] == f"FROM {BASE}"
    assert lines[2] == "USER root"
    assert lines[-1] == "USER ubuntu:ubuntu"


def test_empty_spec_renders_a_valid_noop(tmp_path):
    df = generate_dockerfile(BuildSpec(tag="t"), tmp_path, BASE)
    assert df.splitlines() == ["# syntax=docker/dockerfile:1", f"FROM {BASE}",
                               "USER root", "USER ubuntu:ubuntu"]


# ---------------------------------------------------------------------------
# .dockerignore — the local context must match what the cluster stages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["results", ".git", "__pycache__"])
def test_dockerignore_excludes_at_root_and_nested(name):
    patterns = render_dockerignore().splitlines()
    assert name in patterns
    assert f"**/{name}" in patterns
