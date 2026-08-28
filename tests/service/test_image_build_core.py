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

from robovast.common.build_context import (campaign_outputs_in, is_campaign_output,
                                           render_dockerignore)
from robovast.service.image_build import (BuildSpec, build_hash, classify_build_error,
                                          generate_dockerfile, not_built_message,
                                          primary_build_ref)

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
    """Order does not decide whether a build *works*, but it is what gets built — the
    specs are passed to pip in the order listed — so it stays in the key."""
    a = BuildSpec(tag="t", python_packages=["roqsim", "roqsim_assets"])
    b = BuildSpec(tag="t", python_packages=["roqsim_assets", "roqsim"])
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


def test_each_context_entry_is_copied_before_its_groups_install(tmp_path):
    """Per-entry COPY (not ``COPY .``) so an unrelated file invalidates no pip layer,
    and the group's install follows the copies it needs."""
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "setup.py").write_text("")
    spec = BuildSpec(tag="t", python_packages=["a", "b"])
    lines = [ln for ln in generate_dockerfile(spec, tmp_path, BASE).splitlines()
             if ln.startswith(("COPY", "RUN --mount"))]
    assert lines[0].startswith("COPY a ")
    assert lines[1].startswith("COPY b ")
    assert "/robovast_build_context/a" in lines[2]
    assert "/robovast_build_context/b" in lines[2]


def test_a_source_dir_is_not_installed_editable(tmp_path):
    """`-e` skips `data_files`, which silently breaks every ROS package.

    An editable install routes setuptools through `setup.py develop`, which does not
    install `data_files` -- so an `ament_python` package's
    `share/ament_index/resource_index/packages/<pkg>` marker never lands and
    `ros2 launch <pkg> ...` reports the package as missing, listing a search path that
    contains the prefix it was installed into. It cost a Kinova/MoveIt campaign whose
    move_group never came up. The COPY above already bakes the source into the image, so
    editability had nothing to keep in sync and only this failure to offer.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "setup.py").write_text("")
    spec = BuildSpec(tag="t", python_packages=["pkg"])
    df = generate_dockerfile(spec, tmp_path, BASE)
    assert "/robovast_build_context/pkg" in df
    assert "-e /robovast_build_context/pkg" not in df, (
        "source dirs must install non-editably or data_files are dropped")


# ---------------------------------------------------------------------------
# install groups — one pip resolution pass each
# ---------------------------------------------------------------------------

def _wheels(tmp_path, *names):
    (tmp_path / "wheels").mkdir(exist_ok=True)
    out = []
    for name in names:
        rel = f"wheels/{name}-0.1.0-py3-none-any.whl"
        _wheel(tmp_path / rel, {f"{name}/__init__.py": ""})
        out.append(rel)
    return out


def _installs(spec, tmp_path):
    return [ln for ln in generate_dockerfile(spec, tmp_path, BASE).splitlines()
            if ln.startswith("RUN --mount")]


def test_a_flat_list_is_one_resolution_pass(tmp_path):
    """The default has to be the correct one: pip sees every local wheel at once, so a
    wheel's dependency on a sibling resolves against the sibling. Installed one at a
    time, pip went to the index instead and the build died with
    'No matching distribution found for roqsim_manipulation'."""
    roqsim, manip = _wheels(tmp_path, "roqsim", "roqsim_manipulation")
    # Deliberately dependency-hostile order: the dependent wheel first.
    spec = BuildSpec(tag="t", python_packages=[manip, roqsim, "shapely>=2.0"])

    installs = _installs(spec, tmp_path)

    assert len(installs) == 1
    assert manip in installs[0] and roqsim in installs[0] and "'shapely>=2.0'" in installs[0]


def test_nesting_chooses_the_layer_boundaries(tmp_path):
    """One RUN per group, in order — the author's caching lever."""
    roqsim, assets, glue = _wheels(tmp_path, "roqsim", "roqsim_assets", "glue")
    spec = BuildSpec(tag="t", python_packages=["mujoco>=3.0", [roqsim, assets], glue])

    installs = _installs(spec, tmp_path)

    assert len(installs) == 3
    assert "'mujoco>=3.0'" in installs[0]          # bare string beside a list: group of one
    assert roqsim in installs[1] and assets in installs[1]
    assert glue in installs[2]


def test_grouping_changes_the_hash(tmp_path):
    """Same specs, different layers — the hash is what decides whether to rebuild."""
    one = BuildSpec(tag="t", python_packages=["a>=1", "b>=1"])
    two = BuildSpec(tag="t", python_packages=[["a>=1"], ["b>=1"]])
    assert build_hash(one, tmp_path, BASE) != build_hash(two, tmp_path, BASE)


# ---------------------------------------------------------------------------
# the venv the packages land in
# ---------------------------------------------------------------------------

def test_installs_target_the_venv_not_the_debian_interpreter(tmp_path):
    """Debian's numpy/scipy carry no RECORD, so pip cannot uninstall them and any
    dependency wanting another version killed the build. Inside a venv pip leaves what
    is outside it alone."""
    df = generate_dockerfile(BuildSpec(tag="t", python_packages=["shapely>=2.0"]),
                             tmp_path, BASE)
    assert "--break-system-packages" not in df
    assert "pip --python /usr/local/bin/python3 install" in df


def test_the_venv_is_created_before_any_install_and_only_if_absent(tmp_path):
    lines = generate_dockerfile(
        BuildSpec(tag="t", python_packages=["shapely>=2.0"]), tmp_path, BASE).splitlines()
    venv = next(i for i, ln in enumerate(lines) if "python3 -m venv" in ln)
    install = next(i for i, ln in enumerate(lines) if ln.startswith("RUN --mount"))
    assert venv < install
    # Idempotent: a base that already carries the venv spends a cached layer, not a
    # second venv over the first.
    assert lines[venv].startswith("RUN test -f /usr/local/pyvenv.cfg || (")
    assert "--system-site-packages" in lines[venv]   # ROS's own python stays importable


def test_the_venv_is_handed_back_to_the_system_interpreter(tmp_path):
    """``ros2 launch`` starts nodes with /usr/bin/python3, which does not see a venv.
    Without this .pth the image builds green and the RUN fails on import."""
    df = generate_dockerfile(BuildSpec(tag="t", python_packages=["shapely>=2.0"]),
                             tmp_path, BASE)
    assert "site.addsitedir" in df and "robovast_venv.pth" in df
    # Asked for, not spelled out — the interpreter version is not baked into the image.
    assert "python3.12" not in df


def test_the_ament_path_covers_the_prefix_packages_are_installed_into(tmp_path):
    """So a project never has to set AMENT_PREFIX_PATH to find its own ROS package."""
    df = generate_dockerfile(BuildSpec(tag="t", python_packages=["shapely>=2.0"]),
                             tmp_path, BASE)
    assert "ENV AMENT_PREFIX_PATH=/usr/local" in df
    assert "ENV VIRTUAL_ENV=/usr/local" in df


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
    lines = generate_dockerfile(BuildSpec(tag="t"), tmp_path, BASE).splitlines()
    assert lines[:3] == ["# syntax=docker/dockerfile:1", f"FROM {BASE}", "USER root"]
    assert lines[-1] == "USER ubuntu:ubuntu"
    assert not [ln for ln in lines if ln.startswith(("COPY", "RUN --mount"))]


# ---------------------------------------------------------------------------
# failure classification
# ---------------------------------------------------------------------------

def test_a_base_whose_pip_predates_python_option_says_which_knob():
    """The install targets the venv through the base's own pip. Only a hand-picked
    base_image can be too old for that, and no package edit fixes it."""
    err = classify_build_error("ERROR: no such option: --python\n")
    assert err.entry == "base_image"
    assert "pip >= 22.3" in err.message and "base_image" in err.message


def test_a_missing_distribution_still_names_the_requirement():
    """Installing a group in one pass costs the per-entry attribution, not this: the
    name comes from pip's own message."""
    err = classify_build_error(
        "ERROR: No matching distribution found for roqsim_manipulation\n")
    assert err.phase == "pip" and err.entry == "roqsim_manipulation"


# The line a real failure produced. Both parenthesised clauses matter: the first names
# the distribution that required it, the second is pip's "no candidates" note.
_TRANSITIVE = (
    "#14 3.849 ERROR: Could not find a version that satisfies the requirement roqsim "
    "(from roqsim-mobile-logistics) (from versions: none)\n"
    "#14 3.850 ERROR: No matching distribution found for roqsim\n")


def _spec(**kwargs):
    return BuildSpec(tag="scenario", **kwargs)


def test_a_dependency_of_a_local_package_blames_the_base_image_not_the_list():
    """The regression this exists for.

    ``roqsim`` was never in ``build.python_packages`` -- it is a dependency of the
    project's own package, and it is missing because the image the container builds on
    is the wrong one. Answering "check build.python_packages" sends an agent to edit a
    list that does not contain the name, and cannot.
    """
    err = classify_build_error(_TRANSITIVE, _spec(
        base_image="harbor.example/robovast-roqsim@sha256:abc",
        python_packages=["./", ["./ros2_ws/src/roqsim_mm_bringup"]]))

    assert err.phase == "base-image"
    assert err.entry == "roqsim"
    assert "roqsim-mobile-logistics" in err.message, "say what required it"
    assert "execution.containers" in err.message, "name the field that fixes it"
    assert "sha256:abc" in err.message, "name the image it actually built on"
    # The old advice must not survive anywhere in the message.
    assert "check build.python_packages" not in err.message
    # Still agent-fixable -- just through a different knob. `infra` would say "give up".
    assert err.fixable_by == "agent"


def test_a_declared_package_that_is_missing_still_points_at_the_list():
    """The other half: when the name IS one the author asked for, the list is right."""
    err = classify_build_error(
        "ERROR: Could not find a version that satisfies the requirement roqsim-sensors "
        "(from versions: none)\n",
        _spec(python_packages=["roqsim_sensors>=1.2"]))
    assert err.phase == "pip"
    assert "build.python_packages" in err.message


def test_a_declared_name_is_matched_however_it_was_spelled():
    """pip canonicalises; authors do not. Comparing raw strings would call a declared
    package undeclared over an underscore, and send the agent to the wrong field."""
    err = classify_build_error(
        "ERROR: Could not find a version that satisfies the requirement Roqsim.Sensors "
        "(from versions: none)\n",
        _spec(python_packages=["roqsim_sensors"]))
    assert err.phase == "pip"


def test_without_a_spec_it_claims_nothing_about_the_package_list():
    """The cluster lane passed no spec, so every message was a guess stated as fact.
    With no spec the classifier may still report what pip said -- not where to fix it."""
    err = classify_build_error(_TRANSITIVE)
    assert err.entry == "roqsim"
    assert "is not declared in build.python_packages" not in err.message
    assert "check build.python_packages" not in err.message


def test_the_no_candidates_clause_is_not_read_as_the_requiring_package():
    """pip prints ``(from versions: none)`` on the same line; taking it as the requiring
    distribution would invent a package called ``versions:`` and blame the base image
    for a genuinely undeclared entry."""
    err = classify_build_error(
        "ERROR: Could not find a version that satisfies the requirement nope "
        "(from versions: none)\n", _spec(python_packages=["something-else"]))
    assert err.phase == "pip"
    assert "versions" not in err.message


# ---------------------------------------------------------------------------
# .dockerignore — the local context must match what the cluster stages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["results", ".git", "__pycache__"])
def test_dockerignore_excludes_at_root_and_nested(name):
    patterns = render_dockerignore().splitlines()
    assert name in patterns
    assert f"**/{name}" in patterns


# ---------------------------------------------------------------------------
# Campaign outputs beside the sources
#
# `results/` is ignored, but a `--wait-and-download` can land a campaign at the project
# root, and it is named after the campaign id -- so there is no name to add to the ignore
# set. Recognised by structure instead: a directory holding `_execution/`.
# ---------------------------------------------------------------------------

def _campaign_dir(root, name):
    """A downloaded campaign: the `_execution/` marker plus a heavy `_jobs/` tree."""
    (root / name / "_execution").mkdir(parents=True)
    (root / name / "_execution" / "build.log").write_text("...")
    (root / name / "_jobs" / "batch-0").mkdir(parents=True)
    (root / name / "_jobs" / "batch-0" / "run.bag").write_bytes(b"\0" * 1024)
    return root / name


def test_a_campaign_directory_is_recognised_by_structure_not_by_name(tmp_path):
    campaign = _campaign_dir(tmp_path, "tb4-markerless-2026-08-18-19401887")
    (tmp_path / "src").mkdir()
    assert is_campaign_output(campaign)
    assert not is_campaign_output(tmp_path / "src")


def test_campaign_outputs_are_found_and_not_descended_into(tmp_path):
    _campaign_dir(tmp_path, "campaign-a")
    _campaign_dir(tmp_path / "nested", "campaign-b")
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    found = {str(p) for p in campaign_outputs_in(tmp_path)}
    assert found == {"campaign-a", "nested/campaign-b"}


def test_campaign_outputs_does_not_walk_into_ignored_names(tmp_path):
    """`results/` is already excluded wholesale; walking it would cost the scan it saves."""
    _campaign_dir(tmp_path / "results", "campaign-c")
    assert campaign_outputs_in(tmp_path) == []


def test_dockerignore_lists_the_campaign_directories_found(tmp_path):
    _campaign_dir(tmp_path, "campaign-a")
    patterns = render_dockerignore(tmp_path).splitlines()
    assert "campaign-a" in patterns
    # Still a superset of the static set — the computed part only adds.
    assert set(render_dockerignore().splitlines()) <= set(patterns)


def test_both_lanes_skip_the_same_campaign_directory(tmp_path):
    """The staging and the .dockerignore must agree, or the two builders see different trees."""
    from robovast.execution.cluster_execution.cluster_image_build import _copy_tree

    _campaign_dir(tmp_path, "campaign-a")
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "pkg-0.1.0-py3-none-any.whl").write_bytes(b"wheel")

    staged = tmp_path.parent / "staged"
    _copy_tree(tmp_path, staged)
    assert not (staged / "campaign-a").exists()
    assert (staged / "plugins" / "pkg-0.1.0-py3-none-any.whl").exists()
    assert "campaign-a" in render_dockerignore(tmp_path).splitlines()


def test_a_campaign_directory_does_not_change_the_build_hash(tmp_path):
    """It is not a `python_packages` entry, so it must not move the key either way."""

    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "pkg-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    spec = _spec(python_packages=["./plugins/pkg-0.1.0-py3-none-any.whl"])
    before = build_hash(spec, tmp_path, "base@sha256:aaa")
    _campaign_dir(tmp_path, "campaign-a")
    assert build_hash(spec, tmp_path, "base@sha256:aaa") == before


# ---------------------------------------------------------------------------
# The answers a lane must not phrase for itself
# ---------------------------------------------------------------------------

def _status(phase, **kw):
    from robovast.service.interface import ImageBuildStatus
    return ImageBuildStatus(build_id="build-sut-abc123", tag="sut", phase=phase, **kw)


def test_no_build_known_says_build_it():
    message, next_step = not_built_message("sut", "build-sut-abc123", None)
    assert "not built" in message and "no build is running" in message
    assert next_step == "build_experiment_image(container='sut')"


@pytest.mark.parametrize("phase", ["pending", "validating", "building", "pushing"])
def test_a_build_in_flight_says_wait_not_build(phase):
    """The state an agent actually lands in when it execs straight after a build, and the
    one the old single message could not distinguish from "you forgot to build"."""
    message, next_step = not_built_message(
        "sut", "build-sut-abc123", _status(phase, started_at="2026-08-18T10:00:00Z"))
    assert "still building" in message
    assert "2026-08-18T10:00:00Z" in message      # how long it has been going
    assert "vast image wait build-sut-abc123" in next_step
    assert "build_experiment_image" not in next_step, "a second build is the wrong move"


def test_a_blocked_build_says_neither_wait_nor_build():
    """The state that produced the report: a build exists, but its pod cannot start. Both of
    the neighbouring answers are wrong here -- "still building, wait for it" is what left a
    caller waiting on a pod that was never going to run, and falling through to "no build is
    running, build again" (which is what happened before ``blocked`` existed) sends it to
    rebuild inputs that were never the problem."""
    from robovast.service.interface import ImageBuildError
    message, next_step = not_built_message(
        "sut", "build-sut-abc123",
        _status("blocked",
                error=ImageBuildError(
                    phase="builder-pod", fixable_by="infra",
                    message="the build pod cannot start -- ImagePullBackOff: no basic "
                            "auth credentials")))
    assert "no basic auth credentials" in message
    assert "the cluster, not the project" in message
    assert "still building" not in message
    assert "no build is running" not in message
    assert "build_experiment_image" not in next_step, "a rebuild changes nothing here"


def test_a_failed_build_points_at_the_diagnosis_not_a_retry():
    from robovast.service.interface import ImageBuildError
    message, next_step = not_built_message(
        "sut", "build-sut-abc123",
        _status("failed", done=True,
                error=ImageBuildError(phase="pip", message="no distribution for shapely==99")))
    assert "no distribution for shapely==99" in message
    assert "fails the same way" in message
    assert "get_image_build_status('build-sut-abc123')" in next_step
    assert "summarize=True" in next_step


@pytest.mark.parametrize("phase", ["succeeded", "cached"])
def test_a_vanished_image_says_it_was_built(phase):
    """Built and then pruned is not "never built": denying what demonstrably happened sends
    the reader looking for a mistake they did not make."""
    message, _ = not_built_message("sut", "build-sut-abc123", _status(phase, done=True))
    assert "was built" in message
    assert "no longer" in message


def test_every_refusal_says_a_sibling_hit_proves_nothing():
    for status in (None, _status("building"), _status("failed", done=True)):
        message, _ = not_built_message("sut", "build-sut-abc123", status)
        assert "another container" in message
        assert "never builds implicitly" in message


def test_no_refusal_leaks_a_registry_ref():
    """The whole point of ``identity``: a refusal is client-facing text."""
    for status in (None, _status("building"), _status("succeeded", done=True)):
        message, next_step = not_built_message("sut", "build-sut-abc123", status)
        assert "registry.local" not in message + next_step
        assert "/" not in next_step.replace("build_experiment_image", "")  # no host/path ref


# ---------------------------------------------------------------------------
# One image's cache verdict is not the request's
# ---------------------------------------------------------------------------

def _ref(build_id, cached):
    from robovast.service.interface import ImageBuildRef
    return ImageBuildRef(build_id=build_id, tag=build_id, cached=cached)


def test_a_request_is_cached_only_when_every_image_was():
    """The reported bug: ``cached`` was the primary container's flag, so a scenario cache hit
    reported the whole request as cached while ``sut`` was still building."""
    primary = primary_build_ref({"scenario": _ref("b-scenario", True),
                                 "sut": _ref("b-sut", False)})
    assert primary.build_id == "b-scenario"       # the handle still prefers the scenario
    assert primary.cached is False
    assert primary.cached_builds == {"scenario": True, "sut": False}
    assert primary.builds == {"scenario": "b-scenario", "sut": "b-sut"}


def test_all_cached_is_still_a_cache_hit():
    primary = primary_build_ref({"scenario": _ref("b-scenario", True),
                                 "sut": _ref("b-sut", True)})
    assert primary.cached is True
    assert primary.cached_builds == {"scenario": True, "sut": True}


def test_a_project_without_a_scenario_image_still_gets_a_handle():
    primary = primary_build_ref({"sut": _ref("b-sut", False)})
    assert primary.build_id == "b-sut"
    assert primary.cached is False


# ---------------------------------------------------------------------------
# The base is hashed by identity, not by the ref it was asked for
# ---------------------------------------------------------------------------

def test_rebuilt_base_changes_the_hash(tmp_path):
    """The same ref pointing at new bytes must rebuild.

    This is the property that makes the dated apt archives in the base image reach campaign
    images at all: `make refresh-build-pins` rebuilds the base under an unchanged tag, and if
    the key hashed the tag the store would keep serving an image built against the old
    archives.
    """
    spec = BuildSpec(tag="sim", system_packages=["x11vnc"], python_packages=[])
    before = build_hash(spec, tmp_path, "sha256:" + "a" * 64)
    after = build_hash(spec, tmp_path, "sha256:" + "b" * 64)
    assert before != after


def test_base_identity_falls_back_to_the_ref(monkeypatch):
    """No daemon, no verdict -- and the pre-refinement value rather than an exception.

    `_base_identity` sits on the path to every build decision, cache hits included, so it must
    not fail when docker is absent; the ref is then the honest answer and the behaviour is
    exactly what it was before the identity refinement.
    """
    from robovast.service import image_store

    monkeypatch.setattr(image_store, "local_image_id", lambda _image: "")
    assert image_store.LocalDockerImageStore._base_identity(BASE) == BASE

    monkeypatch.setattr(image_store, "local_image_id", lambda _image: "sha256:" + "c" * 64)
    assert image_store.LocalDockerImageStore._base_identity(BASE) == "sha256:" + "c" * 64


def test_the_walk_survives_a_symlink_loop(tmp_path):
    """A link back to an ancestor must not make the scan run forever."""
    _campaign_dir(tmp_path, "campaign-a")
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    assert [str(p) for p in campaign_outputs_in(tmp_path)] == ["campaign-a"]
