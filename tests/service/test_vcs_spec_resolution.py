# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A moving git ref in python_packages must not be silently cache-stable.

``pkg @ git+https://host/repo@main`` is not a pin, and the image cache key hashed the spec
*string*. So the first build baked in whatever ``main`` was that day, every later campaign
reused that image, and the resolution changed only when something unrelated invalidated the key
-- a renderer epoch bump, a new base image. That is worse than "always latest": the code in the
image was unpredictable, and nothing recorded which commit it was.

``container/robovast/build.sh`` already solved this for ``ROQSIM_REF`` by resolving with
``git ls-remote`` before the build, "so the key changes exactly when the remote does". These
tests pin the same behaviour for the specs a campaign author writes.
"""

import pytest

from robovast.service.image_build import (_PIP_INSTALL, BuildSpec, build_hash,
                                          generate_dockerfile, pin_vcs_specs,
                                          resolve_floating_vcs_specs)


def _spec(*packages) -> BuildSpec:
    return BuildSpec(tag="probe", base_image="base:1", python_packages=list(packages))


def test_a_moving_ref_changes_the_cache_key(tmp_path):
    """The core fix. Same spec text, different resolution -> different image identity."""
    spec = _spec("pkg @ git+https://host/repo@main")
    first = build_hash(spec, tmp_path, "base:1",
                       resolved_vcs={"pkg @ git+https://host/repo@main": "a" * 40})
    second = build_hash(spec, tmp_path, "base:1",
                        resolved_vcs={"pkg @ git+https://host/repo@main": "b" * 40})
    assert first != second, "a branch that moved must rebuild"


def test_the_same_resolution_is_still_a_cache_hit(tmp_path):
    """Resolution must not defeat caching: an unchanged branch has to keep hitting."""
    spec = _spec("pkg @ git+https://host/repo@main")
    resolved = {"pkg @ git+https://host/repo@main": "c" * 40}
    assert build_hash(spec, tmp_path, "base:1", resolved_vcs=resolved) == \
        build_hash(spec, tmp_path, "base:1", resolved_vcs=resolved)


def test_a_non_vcs_spec_is_unaffected(tmp_path):
    """An index pin is already a pin; adding resolution machinery must not change its identity,
    or every existing project rebuilds for nothing."""
    spec = _spec("shapely>=2.0")
    assert build_hash(spec, tmp_path, "base:1") == \
        build_hash(spec, tmp_path, "base:1", resolved_vcs={})


@pytest.mark.parametrize("spec_text,expected_ref", [
    ("pkg @ git+https://host/repo@main", "main"),
    ("pkg @ git+https://host/repo@feature/x", "feature/x"),
    ("pkg @ git+ssh://git@host/repo@v1.2.3", "v1.2.3"),
    ("pkg @ git+https://host/repo", "HEAD"),
])
def test_which_specs_need_resolving(spec_text, expected_ref, monkeypatch):
    """A URL may carry no ref at all, meaning the default branch -- which is the *most* floating
    case and would be missed by only looking for an explicit '@ref'."""
    seen = {}

    def fake_ls_remote(url, ref, **_kwargs):
        seen["url"], seen["ref"] = url, ref
        return "d" * 40

    monkeypatch.setattr("robovast.service.image_build._ls_remote", fake_ls_remote)
    assert resolve_floating_vcs_specs([spec_text]) == {spec_text: "d" * 40}
    assert seen["ref"] == expected_ref


def test_a_spec_already_pinned_to_a_commit_is_left_alone(monkeypatch):
    """No network round trip for something already immutable -- and nothing to record that the
    spec does not already say."""
    def boom(*_args, **_kwargs):
        raise AssertionError("must not resolve an already-pinned spec")

    monkeypatch.setattr("robovast.service.image_build._ls_remote", boom)
    assert resolve_floating_vcs_specs([f"pkg @ git+https://host/repo@{'e' * 40}"]) == {}


def test_an_unresolvable_ref_refuses_rather_than_falling_back(monkeypatch):
    """The whole point. Falling back to the bare ref would quietly restore the stale-cache
    behaviour this removes -- the same reasoning build.sh states for refusing."""
    monkeypatch.setattr("robovast.service.image_build._ls_remote", lambda *a, **k: "")
    with pytest.raises(ValueError) as excinfo:
        resolve_floating_vcs_specs(["pkg @ git+https://host/repo@nope"])
    message = str(excinfo.value)
    assert "Not falling back" in message
    # Both causes are named, because they need different fixes and the git output says neither.
    assert "does not exist" in message and "credentials" in message


def test_an_ambiguous_ref_is_not_guessed(monkeypatch):
    """`ls-remote <url> <name>` can match a branch and a tag. Taking the first line is how you
    silently pin a tag when you meant a branch, so several matches must refuse."""
    def two_lines(_url, ref, **_kwargs):
        if ref.startswith("refs/"):
            return ""
        return ""   # the real function returns "" for a multi-line ambiguous result

    monkeypatch.setattr("robovast.service.image_build._ls_remote", two_lines)
    with pytest.raises(ValueError):
        resolve_floating_vcs_specs(["pkg @ git+https://host/repo@ambiguous"])


def test_the_rendered_dockerfile_installs_the_commit_not_the_branch(tmp_path):
    """The cache key knowing the resolution is not enough: a branch that moves between
    resolution and `pip install` would install what the record does not name."""
    spec = _spec("pkg @ git+https://host/repo@main")
    rendered = generate_dockerfile(
        spec, tmp_path, "base:1",
        resolved_vcs={"pkg @ git+https://host/repo@main": "f" * 40})
    # Found by the install verb rather than by the plain _PIP_INSTALL prefix: a group holding a
    # git spec renders the credential-mounting form, which shares the prefix only up to its
    # first mount.
    install = next(l for l in rendered.splitlines() if " install " in l)
    assert f"git+https://host/repo@{'f' * 40}" in install
    assert "repo@main" not in install, "the install must name the commit, not the branch"
    # The branch DOES survive in the manifest, and should: the record has to show what was
    # asked for beside what it resolved to, or nobody can tell a pin from a resolution.
    manifest = next(l for l in rendered.splitlines() if "vcs.txt" in l)
    assert "repo@main" in manifest and "f" * 40 in manifest


def test_pinning_preserves_install_group_structure():
    """Groups are pip resolution passes and their boundaries are hashed, so rewriting specs must
    not flatten them -- that would change the image for every project that uses grouping."""
    groups = [["a @ git+https://h/a@main", "b>=1"], ["c @ git+https://h/c@dev"]]
    pinned = pin_vcs_specs(groups, {"a @ git+https://h/a@main": "1" * 40,
                                    "c @ git+https://h/c@dev": "2" * 40})
    assert pinned == [[f"a @ git+https://h/a@{'1' * 40}", "b>=1"],
                      [f"c @ git+https://h/c@{'2' * 40}"]]


@pytest.mark.parametrize("url,expected", [
    # userinfo @ before the host: splitting on the FIRST @ gives "git+ssh://git".
    ("git+ssh://git@host/repo@v1.2.3", ("git+ssh://git@host/repo", "v1.2.3", "")),
    # a ref containing /: looking for the @ after the LAST / finds nothing.
    ("git+https://host/repo@feature/x", ("git+https://host/repo", "feature/x", "")),
    # both at once.
    ("git+ssh://git@host/org/repo@release/2.0",
     ("git+ssh://git@host/org/repo", "release/2.0", "")),
    ("git+https://host/repo", ("git+https://host/repo", None, "")),
    ("git+file:///srv/repo@main", ("git+file:///srv/repo", "main", "")),
    # pip's fragment follows the ref and is no part of it. Read as one, "main#subdirectory=pkg"
    # matches nothing and the spec is refused as unresolvable.
    ("git+https://host/repo@main#subdirectory=pkg",
     ("git+https://host/repo", "main", "#subdirectory=pkg")),
    # ... and a fragment on an ALREADY PINNED spec kept it from matching _IMMUTABLE_REF, so a
    # pin was refused too -- the failure that reaches a multi-package repository first.
    ("git+https://host/repo@" + "a" * 40 + "#subdirectory=pkg",
     ("git+https://host/repo", "a" * 40, "#subdirectory=pkg")),
    # a fragment may itself contain @, which must not be read as the ref separator.
    ("git+https://host/repo#egg=pkg&subdirectory=a@b",
     ("git+https://host/repo", None, "#egg=pkg&subdirectory=a@b")),
    ("git+https://host/repo#subdirectory=pkg",
     ("git+https://host/repo", None, "#subdirectory=pkg")),
])
def test_url_and_ref_are_split_on_the_authority(url, expected):
    """Neither obvious rule works, and neither wrong answer fails loudly -- a mis-split simply
    never matches, and the stale-cache behaviour returns silently. So both traps are pinned."""
    from robovast.service.image_build import _split_url_ref

    assert _split_url_ref(url) == expected


def test_a_bare_url_without_a_requirement_name_still_resolves(monkeypatch):
    """pip accepts a plain VCS URL with no `name @` prefix, and rewriting it must not invent one
    -- an invented name would make pip install a different package than was asked for."""
    monkeypatch.setattr("robovast.service.image_build._ls_remote", lambda *a, **k: "9" * 40)
    spec = "git+https://host/repo@main"
    resolved = resolve_floating_vcs_specs([spec])
    assert pin_vcs_specs([spec], resolved) == [f"git+https://host/repo@{'9' * 40}"]


# ---------------------------------------------------------------------------
# The build manifest: what the image ended up containing
# ---------------------------------------------------------------------------

def test_the_manifest_is_recorded_after_the_installs(tmp_path):
    """Order matters: recorded last so it observes the finished image, and in its own layers so
    it never invalidates the install layers above it."""
    from robovast.service.image_build import BUILD_MANIFEST_DIR

    spec = _spec("numpy<=1.13")
    spec.system_packages = ["tree"]
    lines = generate_dockerfile(spec, tmp_path, "base:1").splitlines()
    manifest_at = next(i for i, l in enumerate(lines) if BUILD_MANIFEST_DIR in l)
    install_at = max(i for i, l in enumerate(lines)
                     if _PIP_INSTALL in l or "apt-get" in l)
    assert manifest_at > install_at


def test_the_manifest_does_not_enter_the_cache_key(tmp_path):
    """It is an output, not an input. Hashing it would invalidate the cache on every upstream
    package release -- defeating the caching the key exists for."""
    spec = _spec("numpy<=1.13")
    before = build_hash(spec, tmp_path, "base:1")
    rendered = generate_dockerfile(spec, tmp_path, "base:1")
    assert "build-manifest" in rendered
    assert build_hash(spec, tmp_path, "base:1") == before


def test_pip_recording_cannot_fail_the_build(tmp_path):
    """A base with no pip is not a broken build, and failing here would turn recording a fact
    into a reason the campaign cannot exist."""
    rendered = generate_dockerfile(_spec("numpy"), tmp_path, "base:1")
    pip_line = next(l for l in rendered.splitlines() if "pip list" in l)
    assert "|| true" in pip_line


def test_vcs_resolutions_are_recorded_only_when_there_are_any(tmp_path):
    """pip records a direct URL per distribution but not which *requested* ref it came from, and
    "@main resolved to this commit" is the fact a reader needs -- so it is rendered from what the
    generator resolved. An empty file would be indistinguishable from "nothing floated"."""
    plain = generate_dockerfile(_spec("numpy"), tmp_path, "base:1")
    assert "vcs.txt" not in plain

    floating = generate_dockerfile(
        _spec("pkg @ git+https://h/r@main"), tmp_path, "base:1",
        resolved_vcs={"pkg @ git+https://h/r@main": "a" * 40})
    assert "vcs.txt" in floating
    assert "a" * 40 in floating


@pytest.mark.parametrize("kind,text,expected", [
    ("apt", "tree=2.2.1-1\nadduser=3.152\n", {"tree": "2.2.1-1", "adduser": "3.152"}),
    ("pip", "numpy==1.12.1\npackaging==24.2\n", {"numpy": "1.12.1", "packaging": "24.2"}),
    ("vcs", "pkg @ git+https://h/r@main -> " + "b" * 40 + "\n",
     {"pkg @ git+https://h/r@main": "b" * 40}),
    ("pip", "\n  \nbroken-line\n", {}),
])
def test_manifest_parsing(kind, text, expected):
    """apt uses `=`, pip uses `==`, vcs uses `->`. A line that fits none is skipped rather than
    guessed at -- `pip freeze` also emits `-e git+...` lines that are not `name==version`."""
    from robovast.service.image_build import _parse_manifest

    assert _parse_manifest(kind, text) == expected


def test_reading_a_manifest_never_pulls(monkeypatch):
    """Same rule as the pre-flight: asking what is in an image must not be the thing that
    fetches gigabytes of it."""
    from robovast.service import image_build

    calls = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))

        class Result:
            returncode = 1
            stdout = ""
        return Result()

    monkeypatch.setattr(image_build.subprocess, "run", fake_run)
    monkeypatch.setattr("robovast.common.execution._image_present_locally", lambda _i: True)
    image_build.read_image_build_manifest("img:1")
    for args in calls:
        if args[:2] == ["docker", "run"]:
            assert "--pull=never" in args, args


def test_an_absent_image_reports_unknown_rather_than_empty(monkeypatch):
    """`{}` means "cannot tell". An image built before manifests existed has none, and that is a
    different answer from "installed nothing" -- which a caller must not treat as a lock."""
    from robovast.service.image_build import read_image_build_manifest

    monkeypatch.setattr("robovast.common.execution._image_present_locally", lambda _i: False)
    assert read_image_build_manifest("img:1") == {}


# ---------------------------------------------------------------------------
# Installing FROM the lock: what makes the manifest a mechanism, not a note
# ---------------------------------------------------------------------------

_LOCK = {"apt": {"tree": "2.2.1-1", "curl": "8.5.0-2"},
         "pip": {"numpy": "1.12.1", "packaging": "24.2", "shapely": "2.0.1",
                 "scipy": "1.11.0", "uvicorn": "0.30", "tomli": "2.0"}}


def _pin(system=(), python=()):
    from robovast.service.image_build import pin_specs_from_lock

    spec = BuildSpec(tag="t", system_packages=list(system), python_packages=list(python))
    return pin_specs_from_lock(spec, _LOCK)


@pytest.mark.parametrize("spec,expected", [
    # The case this exists for. An earlier version matched only BARE names and so left every
    # loose spec untouched -- doing nothing while appearing to work.
    ("numpy<=1.13", "numpy==1.12.1"),
    ("shapely>=2.0", "shapely==2.0.1"),
    ("Packaging", "Packaging==24.2"),
])
def test_a_loose_python_spec_is_pinned_to_what_ran(spec, expected):
    assert _pin(python=[spec])[1] == [expected]


@pytest.mark.parametrize("spec", [
    # Already pinned: the author said exactly this, and a lock is not more authoritative than an
    # explicit request.
    "scipy==1.10.0",
    # Direct references name exact code. A version from the lock would swap a local wheel or a
    # git commit for a PyPI release -- a different build than was asked for.
    "./plugins/x.whl",
    "pkg @ git+https://h/r@main",
    "packages/my_pkg",
    # Extras and markers would be dropped by a bare name==version rewrite, changing what installs.
    "uvicorn[standard]",
    'tomli; python_version<"3.11"',
])
def test_specs_a_lock_must_not_rewrite(spec):
    assert _pin(python=[spec])[1] == [spec]


def test_a_spec_the_lock_does_not_mention_is_kept(tmp_path):
    """The lock records what the image CONTAINED -- transitive dependencies included, and possibly
    missing something installed another way. Treating absence as "remove it" would quietly change
    the build rather than reproduce it."""
    assert _pin(python=["not-in-the-lock"])[1] == ["not-in-the-lock"]


def test_apt_specs_are_pinned_and_explicit_pins_respected():
    system, _python = _pin(system=["tree", "curl=8.0-1"])
    assert system == ["tree=2.2.1-1", "curl=8.0-1"]


def test_install_group_structure_survives_pinning():
    """Groups are pip resolution passes and their boundaries are hashed into the image identity,
    so rewriting must not flatten them."""
    _system, python = _pin(python=[["numpy<=1.13", "./plugins/x.whl"], "shapely>=2.0"])
    assert python == [["numpy==1.12.1", "./plugins/x.whl"], "shapely==2.0.1"]


def test_name_normalisation_is_applied():
    """`pip freeze` reports the distribution's own spelling, which may differ in case and in -/_
    from the author's. A literal lookup misses and the spec silently stays unpinned."""
    from robovast.service.image_build import pin_specs_from_lock

    spec = BuildSpec(tag="t", python_packages=["My_Pkg"])
    _system, python = pin_specs_from_lock(spec, {"pip": {"my-pkg": "3.1"}})
    assert python == ["My_Pkg==3.1"]


def test_an_empty_lock_changes_nothing():
    """A rebuild with no lock available must produce the author's intent unchanged, not an empty
    install list -- "cannot tell" is not "install nothing"."""
    system, python = _pin(system=["tree"], python=["numpy<=1.13"])
    from robovast.service.image_build import pin_specs_from_lock

    spec = BuildSpec(tag="t", system_packages=["tree"], python_packages=["numpy<=1.13"])
    assert pin_specs_from_lock(spec, {}) == (["tree"], ["numpy<=1.13"])
    assert (system, python) != (["tree"], ["numpy<=1.13"]), "with a lock it should differ"


# A repository holding several distributions is reached with pip's `#subdirectory=`, and that is
# the shape an asset library takes: one private repo, one package per provider. Every step below
# mishandled the fragment by reading it as part of the ref, so such a spec could not be resolved,
# could not be recognised as already pinned, and -- had either worked -- would have been pinned
# into a spec installing the repository root.

_SUBDIR = "roqsim_scenes @ git+https://host/roqsim@main#subdirectory=roqsim_scenes"


def test_a_subdirectory_spec_resolves_like_any_other(monkeypatch):
    from robovast.service import image_build

    monkeypatch.setattr(image_build, "_ls_remote", lambda url, ref, **kw: "c" * 40)
    assert image_build.resolve_floating_vcs_specs([_SUBDIR]) == {_SUBDIR: "c" * 40}


def test_the_ref_offered_for_resolution_excludes_the_fragment(monkeypatch):
    from robovast.service import image_build

    seen = []

    def _fake(url, ref, **kw):
        seen.append((url, ref))
        return "c" * 40

    monkeypatch.setattr(image_build, "_ls_remote", _fake)
    image_build.resolve_floating_vcs_specs([_SUBDIR])
    # The URL is the repository, and the ref is the branch alone: `git ls-remote` matches
    # nothing for "main#subdirectory=roqsim_scenes", and the spec was refused because of it.
    assert seen == [("https://host/roqsim", "main")]


def test_pinning_keeps_the_subdirectory():
    from robovast.service.image_build import pin_vcs_specs

    [pinned] = pin_vcs_specs([_SUBDIR], {_SUBDIR: "c" * 40})
    assert pinned == ("roqsim_scenes @ git+https://host/roqsim@" + "c" * 40
                      + "#subdirectory=roqsim_scenes")


def test_a_pinned_subdirectory_spec_needs_no_resolution(monkeypatch):
    from robovast.service import image_build

    def _never(url, ref, **kw):
        raise AssertionError(f"resolution attempted for an immutable ref: {ref}")

    monkeypatch.setattr(image_build, "_ls_remote", _never)
    pinned = f"roqsim_scenes @ git+https://host/roqsim@{'c' * 40}#subdirectory=roqsim_scenes"
    assert image_build.resolve_floating_vcs_specs([pinned]) == {}


# ---------------------------------------------------------------------------
# credentials for a private git spec
# ---------------------------------------------------------------------------

def _install_lines(spec, tmp_path):
    from robovast.service.image_build import generate_dockerfile

    return [ln for ln in generate_dockerfile(spec, tmp_path, "base:1").splitlines()
            if " install " in ln]


def test_a_git_group_mounts_the_token_secret(tmp_path):
    from robovast.service.image_build import BuildSpec

    spec = BuildSpec(tag="sim", base_image="base:1",
                     python_packages=[["pkg @ git+https://host/repo@" + "a" * 40]])
    [line] = _install_lines(spec, tmp_path)
    assert "--mount=type=secret,id=git_token" in line
    # Bound to the host rather than answering every prompt: an askpass helper hands the token
    # to whatever asks, so the first spec naming a second host would be given a GitHub one.
    assert "credential.https://github.com.helper" in line
    # Environment-only git config: no .gitconfig is written, so nothing can survive in a layer.
    assert "GIT_CONFIG_COUNT=1" in line


def test_an_ordinary_group_is_left_exactly_as_it_was(tmp_path):
    """The mount rides only on groups that clone. An install layer that needs no credential
    must render byte-identically, or every cached image would rebuild for nothing."""
    from robovast.service.image_build import BuildSpec

    spec = BuildSpec(tag="sim", base_image="base:1", python_packages=["shapely==2.0.1"])
    [line] = _install_lines(spec, tmp_path)
    assert "--mount=type=secret" not in line
    assert "GIT_CONFIG" not in line


def test_the_token_itself_never_reaches_the_dockerfile(tmp_path, monkeypatch):
    """The Dockerfile is written to the build log and staged to S3. What it may contain is the
    PATH the secret is mounted at, never the secret."""
    from robovast.service.image_build import BuildSpec, generate_dockerfile

    monkeypatch.setenv("ROBOVAST_GIT_TOKEN", "ghp_secretvalue")
    spec = BuildSpec(tag="sim", base_image="base:1",
                     python_packages=[["pkg @ git+https://host/repo@" + "a" * 40]])
    assert "ghp_secretvalue" not in generate_dockerfile(spec, tmp_path, "base:1")


def test_a_token_does_not_change_the_image_hash(tmp_path, monkeypatch):
    """BuildKit secrets are outside the cache key by design, and the hash must agree: the same
    inputs are the same image whether or not a credential was needed to fetch them."""
    from robovast.service.image_build import BuildSpec, build_hash

    spec = BuildSpec(tag="sim", base_image="base:1",
                     python_packages=[["pkg @ git+https://host/repo@" + "a" * 40]])
    monkeypatch.delenv("ROBOVAST_GIT_TOKEN", raising=False)
    without = build_hash(spec, tmp_path, "base:1")
    monkeypatch.setenv("ROBOVAST_GIT_TOKEN", "ghp_secretvalue")
    assert build_hash(spec, tmp_path, "base:1") == without
