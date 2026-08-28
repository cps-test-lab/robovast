# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve the ``.vast`` ``plugins:`` list into a per-workspace virtual environment.

A ``.vast`` may declare the plugin packages it needs as pip requirement specs --
typically a **git URL** (``scenario_mt @ git+https://host/repo@ref``), an **index pin**
(``scenario_mt==1.2.3``), or a **workspace-relative wheel** uploaded into the project.
The names it then uses (``SemanticGeneration``, ``optuna``, ...) resolve through the
``robovast.*`` entry-point groups, so the package must be importable wherever those
names are resolved.

:func:`ensure_workspace_plugins` installs the declared specs into a venv under
``<vast_dir>/.robovast_plugins/`` and puts that venv's ``site-packages`` on
``sys.path``; a ``.installed`` marker short-circuits a matching set.

**Why a venv, and not** ``pip install --target``. A plugin is loaded *into robovast's
own process*, so it has to be resolved **against** the host environment rather than in
isolation from it -- and ``--target`` cannot do that: pip forces ``--ignore-installed``
whenever ``--target`` is given, so every dependency is re-materialized whether the host
has it or not. A plugin that (however unnecessarily) declared a dependency on
``robovast`` therefore got a **second robovast** installed beside it; and because
``importlib.metadata`` deduplicates distributions by name and keeps the first on
``sys.path``, that copy's entry points became the only ones the process could see. A
stale one registering no ``robovast.search_strategies`` emptied the group for every
campaign in the service, including those that declared no plugins at all.

Inside a venv pip resolves against what the host already provides, so the host's own
distributions are satisfied and never reinstalled and the venv receives only what is
genuinely missing. It is also what keeps the install **non-invasive**: pip declines to
uninstall what lives outside the environment it targets ("Not uninstalling X ... outside
environment"). That is the protection ``--target`` was buying with ``--ignore-installed``
and that ``--prefix`` would not buy at all. Same mechanism, same reason, as the
experiment image build -- see ``robovast.service.image_build._VENV_SETUP``.

The host is made visible with an explicit ``.pth`` rather than ``--system-site-packages``
alone; :func:`_ensure_venv` explains why that flag is not sufficient.

**Where the plugin is imported.** Composition runs in an isolated subprocess
(``config_generation._compose_isolated``) -- a fresh, short-lived process whose
``sys.path`` this may safely *lead*, which is what lets a plugin's deliberately pinned
dependency win over a different version the host also ships. The remaining consumers --
postprocessing plugins, search **extractors** and search strategies -- resolve
*in-process*, in the service itself. They go through :func:`ensure_plugins_importable`,
which **appends** instead: in a long-lived, multi-workspace process one workspace must
not reorder imports for the next, and a plugin pinning something the service has already
imported could not win anyway. That is the price of resolving a plugin in a long-lived
process, and it is why only the consumers that *cannot* fork do it.
"""

import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import subprocess  # nosec B404 - pip on trusted, config-declared specs
import sys
import sysconfig
import venv
from importlib.metadata import PackageNotFoundError, distributions, version

logger = logging.getLogger(__name__)

#: Per-workspace directory holding the venv the declared plugins are installed into.
#: Lives next to the ``.vast``, and is rebuilt wherever a campaign is composed -- it is
#: excluded from workspace pushes and from image build contexts, so it never travels.
PLUGIN_DIRNAME = ".robovast_plugins"

#: Marker file (inside :data:`PLUGIN_DIRNAME`) holding the hash of the specs that
#: were installed, so a matching set is not reinstalled.
MARKER_NAME = ".installed"

#: The venv, inside :data:`PLUGIN_DIRNAME`, that the declared specs are installed into.
#: A subdirectory rather than the plugin dir itself, so a flat ``pip --target`` tree left
#: by a robovast that predates the venv is simply not on the path any more --- see
#: :func:`_reclaim_pre_venv_layout`.
VENV_DIRNAME = "venv"

#: Written into the venv's ``site-packages`` to hand it the *running* interpreter's site
#: directories. See :func:`_ensure_venv` for why this is not ``--system-site-packages``.
HOST_PTH_NAME = "_robovast_host.pth"


def _self_import_root() -> str:
    """The top-level import package this module belongs to (``robovast``).

    Asked of ``__name__`` rather than written out, so the "is this the host?" tests below
    stay correct under a rename or a vendored fork, and there is no name to drift.
    """
    return __name__.partition(".")[0]


def plugin_dir(vast_dir: str) -> str:
    """The workspace's plugin directory. May not exist."""
    return os.path.join(os.path.abspath(vast_dir), PLUGIN_DIRNAME)


def _venv_dir(vast_dir: str) -> str:
    return os.path.join(plugin_dir(vast_dir), VENV_DIRNAME)


def _venv_python(venv_dir: str) -> str:
    return os.path.join(venv_dir, "bin", "python")


def _purelib_of(venv_dir: str) -> str:
    """The ``site-packages`` of *venv_dir*, without running its interpreter.

    The ``venv`` scheme is asked rather than the path being spelled out, so the
    interpreter version is not baked in and a distro-patched ``posix_prefix`` (Debian
    installs to ``dist-packages``) cannot make this disagree with what ``venv`` created.
    """
    return sysconfig.get_paths(scheme="venv", vars={
        "base": venv_dir, "platbase": venv_dir,
        "installed_base": venv_dir, "installed_platbase": venv_dir})["purelib"]


def plugin_site_dir(vast_dir: str) -> str:
    """The single answer to "which directory is importable for this workspace".

    Every caller that reads the workspace's installed plugin metadata, or puts it on
    ``sys.path``, goes through here, so the layout is defined in exactly one place.
    """
    return _purelib_of(_venv_dir(vast_dir))


def _staged_entry_points(site_dir: str):
    """Entry points the workspace's installed plugins register, without importing them.

    Entry-point names are distribution metadata, so they can be read straight out of the
    directory: no ``sys.path`` change, no import of plugin code, no pip, no network. What
    this cannot confirm is that the class behind a name is valid --- that needs the
    import, and composition is where it happens.
    """
    if not os.path.isdir(site_dir):
        return
    try:
        for dist in distributions(path=[site_dir]):
            yield from dist.entry_points
    except Exception as e:  # noqa: BLE001 - a metadata read must never fail validation
        logger.debug("Could not read staged plugin entry points in %s: %s", site_dir, e)


def _registers_plugins(site_dir: str) -> bool:
    """Whether *site_dir* holds a distribution registering any ``robovast.*`` group."""
    prefix = _self_import_root() + "."
    return any(ep.group.startswith(prefix) for ep in _staged_entry_points(site_dir))

# GitHub credential for installing a private-repo (``git+https``) plugin declared
# in ``plugins:``. **Security:** the token must not be accessible to any workspace
# or command, so it is *never* placed in the process environment, in
# ``~/.gitconfig``, on a command line (visible via ``ps``), or written into a
# workspace/pod. It is delivered as a **mounted secret file** and handed to git
# only for the specific ``pip install`` subprocess via a transient ``GIT_ASKPASS``
# helper that reads the file. A single ``ROBOVAST_GIT_TOKEN`` env var is accepted
# only as a local/dev fallback (single-tenant ``vast serve``).
#:
#: Fixed mount path of the git-token secret (see ``service_deploy``). Not exposed
#: as an env var, so in-process composition code has no obvious handle to it.
GIT_TOKEN_FILE = "/var/run/secrets/robovast-git/token"

#: Local/dev fallback only. In a shared service prefer the mounted file — an env
#: var is inherited by every child process/command. ``ROBOVAST_GIT_TOKEN`` is the
#: canonical name; the conventional ``GITHUB_TOKEN`` / ``GH_TOKEN`` are also
#: accepted so a token already exported for ``gh``/CI is used without renaming.
GIT_TOKEN_ENV = "ROBOVAST_GIT_TOKEN"

#: The full set of host env vars a GitHub token may come from, most-specific
#: first. This is the single source of truth shared with the cluster setup
#: (``service_deploy._GIT_TOKEN_HOST_ENVS``), so local composition and the cluster
#: accept the *same* names and a ``git+https`` plugin install authenticates
#: identically in either place.
GIT_TOKEN_ENVS = (GIT_TOKEN_ENV, "GITHUB_TOKEN", "GH_TOKEN")


def _read_git_token() -> str:
    """Return the configured GitHub token, or ``""``.

    Common to both environments: the mounted secret file is preferred (the cluster
    path), then any of :data:`GIT_TOKEN_ENVS` from the host env (the local path).
    """
    try:
        with open(GIT_TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
            if tok:
                return tok
    except OSError:
        pass
    for var in GIT_TOKEN_ENVS:
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return ""


def _git_askpass_env(token: str, workdir: str) -> dict:
    """Write a throwaway ``GIT_ASKPASS`` helper and return the env overlay to use it.

    The helper simply echoes the token, so git obtains the credential over its
    prompt channel (stdout of the helper) — never on a command line, never in the
    persistent environment, never in ``~/.gitconfig``. The overlay is applied only
    to the ``pip install`` subprocess; the token lives in the helper file (mode
    0700 dir, 0500 file) for the duration of that one install and is removed after.
    """
    helper = os.path.join(workdir, "askpass.sh")
    with open(helper, "w", encoding="utf-8") as f:
        # Any prompt (Username/Password) → the token. GitHub accepts a PAT as the
        # username, which authenticates https clones of private repos.
        f.write("#!/bin/sh\nprintf '%s' \"$ROBOVAST__GIT_TOKEN\"\n")
    os.chmod(helper, 0o500)  # nosec B103 - owner-only exec, transient
    return {
        "GIT_ASKPASS": helper,
        "GIT_TERMINAL_PROMPT": "0",   # never fall back to an interactive prompt
        "ROBOVAST__GIT_TOKEN": token,  # read by the helper; subprocess-scoped only
    }


def _spec_hash(specs) -> str:
    """Stable hash of the declared specs (order-independent) **and of the environment**.

    The environment belongs in it because the install is resolved *against the host*: the
    same specs against a different interpreter, or a different host environment, are a
    different result. Without this a tree built by a 3.12 service was reused verbatim by a
    3.13 one --- marker matching, no reinstall, and a ``site-packages`` that interpreter
    cannot import.
    """
    joined = "\n".join([*sorted(specs),
                        f"python={sys.version_info.major}.{sys.version_info.minor}",
                        f"host={sysconfig.get_paths()['purelib']}"])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()  # nosec B324 - not security


def _requirement_name(spec: str) -> str:
    """Best-effort distribution name for a pip requirement *spec*."""
    try:
        from packaging.requirements import Requirement  # pylint: disable=import-outside-toplevel
        return Requirement(spec).name
    except Exception:  # pylint: disable=broad-except
        head = spec.split("@", 1)[0].strip()
        for op in ("===", "==", ">=", "<=", "~=", "!=", ">", "<"):
            if op in head:
                head = head.split(op, 1)[0]
                break
        return head.strip()


def resolved_plugin_versions(vast_dir: str, specs) -> dict:
    """What the declared plugin *specs* actually resolved to, for the campaign's record.

    A ``plugins:`` entry is usually not a pin. ``scenario_mt @ git+https://host/repo@main``
    resolves to different code every week, and the only thing recorded today is a hash of the
    **specs** (:data:`MARKER_NAME`), which is identical across all of those resolutions. So a
    re-run a year from now installs something else and nothing says so.

    Returns ``{distribution: {...}}`` with, per plugin:

    ``requested``
        the spec as authored, so the record shows intent beside outcome.
    ``version``
        the installed version, read from the workspace's own install dir rather than from
        ``sys.path`` -- the process may have a different copy of the same distribution
        already imported, which is exactly what ``_warn_if_already_loaded`` reports.
    ``host_dependency``
        the requirement, when the plugin declares a dependency on robovast itself. Recorded
        because that declaration is the shape the module docstring warns about, and a reader
        should see it without re-deriving it.
    ``commit`` / ``url``
        for a VCS install, the resolved commit and origin, from the ``direct_url.json`` pip
        writes per PEP 610. This is what turns ``@main`` into something re-installable.
    ``resolved``
        False when the distribution is not in the install dir at all -- an entry that was
        already importable from elsewhere, so pip installed nothing. Recorded rather than
        omitted, because "declared but not resolved here" is a fact a re-run needs.
    """
    site_dir = plugin_site_dir(vast_dir)
    installed = {}
    if os.path.isdir(site_dir):
        for dist in distributions(path=[site_dir]):
            name = (dist.metadata["Name"] or "").strip()
            if name:
                installed[canonical_name(name)] = dist

    record = {}
    for spec in specs or []:
        name = _requirement_name(spec)
        if not name:
            continue
        entry = {"requested": spec}
        dist = installed.get(canonical_name(name))
        if dist is None:
            entry["resolved"] = False
        else:
            entry["version"] = dist.version
            entry.update(_direct_url_origin(dist))
            host_req = _host_dependency_of(dist)
            if host_req:
                entry["host_dependency"] = host_req
        record[name] = entry
    return record


def _host_dependency_of(dist) -> str:
    """The requirement by which *dist* depends on robovast itself, or ``""``.

    Read off the installed metadata rather than parsed out of pip's output, so a
    ``git+https`` spec that pip logged under a different display name is still attributed
    to the distribution that actually declared it.
    """
    root = canonical_name(_self_import_root())
    try:
        requires = dist.metadata.get_all("Requires-Dist") or []
    except Exception:  # pylint: disable=broad-except - provenance must not fail a campaign
        return ""
    for req in requires:
        if canonical_name(_requirement_name(req)) == root:
            return req.strip()
    return ""


def host_dependent_plugins(vast_dir: str) -> dict:
    """``{distribution: requirement}`` for installed plugins that depend on robovast.

    Harmless here --- the host satisfies it, so pip installs nothing --- but it is the
    declaration that would otherwise produce a second robovast in the workspace, and it
    drags the published robovast's own closure into any environment that resolves it
    without the host present. The plugin's author is the only one who can remove it, so
    both the install and ``validate_project`` report it, from this one read.
    """
    site_dir = plugin_site_dir(vast_dir)
    if not os.path.isdir(site_dir):
        return {}
    found = {}
    for dist in distributions(path=[site_dir]):
        req = _host_dependency_of(dist)
        if req:
            found[dist.metadata["Name"] or "?"] = req
    return found


def _warn_host_dependencies(vast_dir: str) -> None:
    """Log :func:`host_dependent_plugins` at install time."""
    for name, req in host_dependent_plugins(vast_dir).items():
        logger.warning(
            "Plugin %r declares %r. A plugin is loaded INTO robovast's process, not "
            "installed beside it, so the host always provides %s and the dependency is "
            "redundant; drop it from the plugin's metadata.",
            name, req, _self_import_root())


def canonical_name(name: str) -> str:
    """PEP 503 name normalisation, so ``robovast_nav`` and ``robovast-nav`` match.

    Public because three callers compare distribution names and must agree on what
    "the same distribution" means: this module, the image build's declared-vs-missing
    check, and the shadowing diagnosis in ``plugin_ref``.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def providers_from_records(records, groups) -> dict:
    """``{distribution: {...}}`` for the providers in *records*, filtered to *groups*.

    *records* are the per-container ``distributions_<name>.json`` files a run's containers
    wrote -- every distribution they hold, with its version, the entry-point groups it
    registers, and its PEP 610 direct URL. This reduces them to the shape the publication gate
    and the retrigger check already read, so neither learns where the record came from.

    A UNION across containers, because providers legitimately differ per role: a ``sut`` image
    has none and the simulation image has them all, and in the ROS shape they are not even the
    same image. The question the record answers is campaign-level -- can someone else obtain
    the code this campaign depended on -- so a provider used by any container belongs in it.
    The per-container files stay beside the record for anyone who needs the detail.

    Where two containers disagree about a version, the first wins and the merge is not
    reported: that would be a campaign running two images built from different asset commits,
    which is worth knowing but is not what this record is for.
    """
    wanted = {group for group in (groups or []) if group}
    if not wanted:
        return {}
    out: dict = {}
    for record in records or []:
        for name, info in sorted((record or {}).items()):
            if not isinstance(info, dict):
                continue
            hit = sorted(set(info.get("groups") or []) & wanted)
            if not hit:
                continue
            entry = out.setdefault(name, {"version": info.get("version") or "",
                                          "groups": []})
            entry["groups"] = sorted(set(entry["groups"]) | set(hit))
            for key, value in _origin_from_direct_url(info.get("direct_url")).items():
                entry.setdefault(key, value)
    return out


def _origin_from_direct_url(data) -> dict:
    """``{commit, url}`` from an already-parsed PEP 610 mapping.

    The same reduction :func:`_direct_url_origin` performs on a distribution installed in
    *this* process -- kept beside it deliberately, because the two must agree: that one reads
    the file through importlib for a plugin installed here, this one reads a copy a container
    wrote, and a reader of either record cannot tell which produced it and must not need to.
    """
    if not isinstance(data, dict):
        return {}
    origin = {}
    url = data.get("url")
    if url:
        origin["url"] = url
    commit = (data.get("vcs_info") or {}).get("commit_id")
    if commit:
        origin["commit"] = commit
    return origin


def _direct_url_origin(dist) -> dict:
    """``{commit, url}`` from a distribution's PEP 610 ``direct_url.json``, if it has one.

    Only a direct (VCS/archive/local) install has this file; an index install does not, and
    for one the version alone is a sufficient pin. Read defensively: this is provenance, so a
    malformed file must not fail the campaign that was going to record it.
    """
    try:
        raw = dist.read_text("direct_url.json")
        if not raw:
            return {}
        data = json.loads(raw)
    except Exception:  # pylint: disable=broad-except
        return {}
    origin = {}
    url = data.get("url")
    if url:
        origin["url"] = url
    commit = (data.get("vcs_info") or {}).get("commit_id")
    if commit:
        origin["commit"] = commit
    return origin


#: Substrings that identify a *credential* failure in a failed install's output, in three
#: groups because they are three different situations with one remedy.
#:
#: Matched against the WHOLE output rather than its tail: git prints the cause first and pip
#: appends its own epilogue after it, so the last lines are "did not run successfully" and
#: "See above for output" while the reason is further up. Diagnosing off the tail is how a
#: private repository the service could not read was reported as "check that each spec is
#: reachable" -- advice for a typo, on a spec that was spelled correctly.
_AUTH_SIGNATURES = (
    # (1) No credential was offered. git falls back to prompting, and a non-interactive
    # install has prompting disabled.
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "fatal: could not read",
    # (2) A credential was offered and rejected.
    "authentication failed",
    "invalid username or password",
    "http basic: access denied",
    # (3) A credential was accepted but does not cover this repository -- the case a token
    # scoped to a different organisation produces, and the easiest to misread. It is an
    # HTTP status rather than a sentence, and git spells it "returned error: 403", so
    # matching only "403 forbidden" missed every real occurrence.
    "access to repository not granted",
    "403 forbidden",
    "returned error: 403",
    "returned error: 401",
)

#: The same class as group (3), kept separate because it is genuinely ambiguous: GitHub
#: answers a private repository with 404 for a requester that may not see it, so this is
#: either a credential that does not cover the repository *or* a URL that names nothing.
#: Both are worth saying, and saying only one of them sends the reader the wrong way.
_NOT_FOUND_SIGNATURES = (
    "repository not found",
    "remote: not found",
    "returned error: 404",
)


def _diagnose_pip_failure(specs, stderr: str) -> str:
    """Turn a failed ``pip install`` into an actionable, MCP-user-facing message."""
    low = (stderr or "").lower()
    auth = any(sig in low for sig in _AUTH_SIGNATURES)
    not_found = any(sig in low for sig in _NOT_FOUND_SIGNATURES)
    lines = [f"Failed to install variation plugin(s) {list(specs)}."]
    if auth or not_found:
        if not_found and not auth:
            lines.append(
                "The source could not be found. For a private repository that is what a "
                "requester without access is told, so this is most likely a credential "
                "that does not cover it rather than a wrong URL -- check both.")
        else:
            lines.append("The source needs credentials (private repository).")
        lines.append(
            "Provide a GitHub token at 'vast cluster setup' so the service can "
            "authenticate, or upload a pre-built wheel into the workspace and reference "
            "it by a workspace-relative path in 'plugins:'. A token only reaches what it "
            "is scoped to: one issued for a different organisation authenticates and then "
            "fails on the repository, which is this same failure.")
    else:
        lines.append(
            "The source could not be installed. Check that each spec is reachable "
            "and declares its own dependencies, or upload a pre-built wheel into the "
            "workspace and reference it by a workspace-relative path in 'plugins:'.")
    if stderr:
        lines.append("pip said:\n" + _failure_excerpt(stderr))
    return "\n".join(lines)


def _failure_excerpt(stderr: str, tail: int = 8) -> str:
    """The tail of *stderr*, plus any line that names a cause the tail would have cut.

    pip's epilogue ("did not run successfully", "See above for output") is what the last
    lines hold, so a plain tail can show the reader everything except the reason. Any line
    carrying a diagnosed signature is kept in its original order, deduplicated against the
    tail, and marked so it is clear the excerpt is not contiguous.
    """
    lines = stderr.strip().splitlines()
    tail_lines = lines[-tail:]
    sigs = _AUTH_SIGNATURES + _NOT_FOUND_SIGNATURES
    cause = [ln for ln in lines[:-tail] if any(s in ln.lower() for s in sigs)]
    if not cause:
        return "\n".join(tail_lines)
    return "\n".join([*cause[-tail:], "  ...", *tail_lines])


def _install_into(venv_dir: str, specs) -> None:
    """``pip install`` the specs into the workspace venv (with dependencies).

    The outer pip targets the venv through ``--python`` rather than the venv running a
    pip of its own --- the same form the image build uses
    (``image_build._PIP_INSTALL``), and why :func:`_ensure_venv` can skip ``ensurepip``.

    Resolution happens against the host (see the module docstring), so a requirement the
    host already satisfies installs nothing, and a genuine conflict is pip's error to
    report here rather than an import-order accident later.

    A configured GitHub token (mounted file, or the local/dev env fallback) is supplied to
    git for this one subprocess only, via a transient ``GIT_ASKPASS`` helper in an
    owner-only temp dir --- never in the parent environment, gitconfig, or a command line.
    """
    import tempfile

    env = dict(os.environ)
    # Never leak a fallback token into the child via the inherited environment;
    # it is re-supplied below only through the scoped GIT_ASKPASS overlay.
    for _tok_var in GIT_TOKEN_ENVS:
        env.pop(_tok_var, None)

    token = _read_git_token()
    askpass_dir = None
    if token:
        askpass_dir = tempfile.mkdtemp(prefix="robovast_git_")
        os.chmod(askpass_dir, 0o700)  # nosec B103 - owner-only, transient
        env.update(_git_askpass_env(token, askpass_dir))

    # Stream pip's output live: a git+https install clones the repo (and any git
    # dependencies), which can take a while, so echo each line as it arrives rather
    # than capturing silently and only surfacing it on failure. The lines are still
    # accumulated so a failure gets the same actionable diagnosis as before.
    cmd = [sys.executable, "-m", "pip", "--python", _venv_python(venv_dir), "install",
           "--root-user-action=ignore", "--disable-pip-version-check",
           # git clones write progress with '\r'; ask for verbose line-based
           # progress so the long clone/build phase is visible while piped.
           "-v", "--progress-bar", "off", *specs]
    output_lines: list[str] = []
    try:
        try:
            proc = subprocess.Popen(  # nosec B603 - specs come from the trusted campaign .vast
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env,
            )
        except FileNotFoundError as exc:  # pip / python missing
            raise RuntimeError(
                f"Could not run pip to install variation plugin(s) {list(specs)}: {exc}"
            ) from exc

        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            output_lines.append(line)
            logger.info("pip: %s", line)
            print(f"  pip | {line}", flush=True)
        returncode = proc.wait()
    finally:
        if askpass_dir:
            shutil.rmtree(askpass_dir, ignore_errors=True)

    if returncode != 0:
        raise RuntimeError(_diagnose_pip_failure(specs, "\n".join(output_lines)))


def _resolve_local_specs(vast_dir: str, specs) -> list:
    """Make workspace-relative wheel/sdist paths absolute against *vast_dir*.

    ``plugins:`` documents "a workspace-relative path to a wheel you uploaded
    ('./plugins/my_plugin-1.0-py3-none-any.whl')", and the relative part is the whole
    point: the author cannot know where the service unpacked the workspace. pip,
    however, resolves a relative path against the *process* CWD -- which for a
    long-lived service is its install dir, so the documented form looked for the wheel
    in ``/opt/robovast/plugins/`` and failed with a FileNotFoundError naming a path the
    user never wrote.

    Only leading-``./``/``../`` forms are touched. An index pin (``foo==1.2``), a git
    URL and an absolute path are all passed through untouched -- a bare ``foo`` must
    keep meaning the package ``foo``, never a directory that happens to share its name.
    """
    def _resolve(path: str) -> str:
        return os.path.normpath(os.path.join(os.path.abspath(vast_dir), path))

    out = []
    for spec in specs:
        text = str(spec).strip()
        if text.startswith(("./", "../")):
            out.append(_resolve(text))
        elif " @ " in text:
            # PEP 508 direct reference: ``name @ <url-or-path>``.
            name, _, ref = text.partition(" @ ")
            ref = ref.strip()
            out.append(f"{name.strip()} @ {_resolve(ref)}"
                       if ref.startswith(("./", "../")) else text)
        else:
            out.append(text)
    return out


def _warn_if_already_loaded(specs) -> None:
    """Warn for each declared plugin already importable from *elsewhere*.

    Called before the workspace dir is put on ``sys.path``, so a hit means the
    distribution is visible from another location (e.g. another workspace already
    served in this long-lived process). With in-process first-wins semantics the
    already-loaded copy is what runs, so surface it instead of silently diverging.

    Not a hit for the host's own distributions: those are what the install *resolves
    against*, so pip has already satisfied them and there is nothing to diverge from.
    :func:`_warn_host_dependencies` reports that case, once, in its own terms.
    """
    for spec in specs:
        name = _requirement_name(spec)
        if not name:
            continue
        try:
            existing = version(name)
        except PackageNotFoundError:
            continue
        logger.warning(
            "Variation plugin %r (version %s) is already present in this process; "
            "the workspace's declared '%s' may not take effect until the service "
            "restarts (in-process, first-wins).", name, existing, spec)


def _is_importable(spec: str) -> bool:
    """Whether *spec*'s distribution is already installed in the current env."""
    name = _requirement_name(spec)
    if not name:
        return False
    try:
        version(name)
        return True
    except PackageNotFoundError:
        return False


def _add_sys_path(site_dir: str, position: str = "prepend") -> None:
    """Put *site_dir* on ``sys.path`` and refresh import caches.

    ``prepend`` lets the workspace's pinned plugin dependencies win --- a plugin may need
    a *different* version of a package the environment also ships (``rdflib``, ``pyld``,
    ...), and a mismatch silently breaks it (a newer ``rdflib`` drops remote JSON-LD
    ``@context`` triples the plugin relies on). That is only safe in a process that exists
    to compose one project: the **isolated compose subprocess** and the aux discovery
    worker.

    ``append`` is for the long-lived service. ``sys.path`` there is process-global and
    shared by every concurrently-running campaign, so leading it with one workspace's
    directory reorders imports for campaigns that never declared a plugin at all. Nothing
    is given up by appending: a plugin cannot outrank a module the service has already
    imported whatever the order, which is the caveat the module docstring states.
    """
    if site_dir in sys.path:
        sys.path.remove(site_dir)
    if position == "prepend":
        sys.path.insert(0, site_dir)
    else:
        sys.path.append(site_dir)
    importlib.invalidate_caches()


def _ensure_venv(venv_dir: str) -> str:
    """Create the workspace venv if absent; return its ``site-packages``.

    Idempotent on the venv, but the host ``.pth`` is rewritten every time, so a workspace
    carried to a different interpreter or a moved host environment is corrected instead of
    silently resolving against the wrong one.

    ``with_pip=False`` because the outer pip installs into it through ``--python``:
    ``ensurepip`` needs a working ``python3-venv`` (absent on some bases) and would cost a
    bootstrap per workspace for a pip that is never used.

    **Why the .pth and not just** ``system_site_packages``. That flag adds
    ``sys.base_prefix``'s site directories --- the *base* interpreter's. When robovast
    itself runs from a venv, which is every developer checkout, the base prefix is the
    system Python and does not have robovast; pip would resolve ``robovast>=1.0.0`` against
    nothing and install a second copy, which is precisely the shadowing this module exists
    to prevent. Naming the **running** interpreter's directories is correct in both
    deployments. ``site.addsitedir`` rather than a bare path line, because it also
    processes the nested ``.pth`` files an editable install writes --- without it an
    editable robovast is on the path but not importable.
    """
    if not os.path.isfile(os.path.join(venv_dir, "pyvenv.cfg")):
        venv.EnvBuilder(system_site_packages=True, with_pip=False).create(venv_dir)
    purelib = _purelib_of(venv_dir)
    os.makedirs(purelib, exist_ok=True)
    host = sysconfig.get_paths()
    with open(os.path.join(purelib, HOST_PTH_NAME), "w", encoding="utf-8") as f:
        for site_dir in dict.fromkeys([host["purelib"], host["platlib"]]):
            f.write(f"import site; site.addsitedir({site_dir!r})\n")
    return purelib


def _reclaim_pre_venv_layout(vast_dir: str) -> None:
    """Remove a flat ``pip --target`` tree left by a robovast that predates the venv.

    Such a tree is already inert --- the importable directory moved into the venv, so it
    is no longer on ``sys.path`` --- but a stale one runs to roughly a gigabyte, and
    leaving it would make ``.robovast_plugins/`` mean two different things at once. The
    marker goes with it: it describes an install this layout no longer has.
    """
    base = plugin_dir(vast_dir)
    if not os.path.isdir(base):
        return
    entries = os.listdir(base)
    if not any(e.endswith(".dist-info") for e in entries):
        return
    logger.warning(
        "Removing a pre-venv plugin tree in %s: plugins are now installed into a venv "
        "resolved against the host, so this one is no longer imported by anything.", base)
    for entry in entries:
        if entry == VENV_DIRNAME:
            continue
        path = os.path.join(base, entry)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass


def staged_variation_type_names(vast_dir: str) -> set:
    """Variation-type names the workspace's installed plugins register, without importing.

    For the one caller that must answer "is this variation name real?" while forbidden
    from finding out the usual way: validation cannot lead ``sys.path`` with the workspace
    and load the entry points, and :func:`ensure_workspace_plugins` would pip-install
    anything not yet there. Reads distribution metadata straight out of the directory ---
    see :func:`_staged_entry_points`.

    Returns an empty set when nothing is installed or nothing registers such a type.
    """
    return {ep.name for ep in _staged_entry_points(plugin_site_dir(vast_dir))
            if ep.group == "robovast.variation_types"}


def ensure_workspace_plugins(vast_dir: str, specs, force: bool = False,
                             add_to_path: bool = True,
                             position: str = "prepend") -> str | None:
    """Ensure the ``.vast``'s ``plugins:`` are installed and importable.

    Two modes:

    * **compose** (``force=False``, the default --- used at the composition
      convergence): a plugin **already installed in the environment** is used as-is.
      This is the ``vast`` CLI path: install the plugin yourself (``pip install`` /
      ``make venv``) and it is *detected*, not re-fetched. Only the declared specs that
      are not importable are installed into the workspace venv. A matching ``.installed``
      marker short-circuits to just adjusting ``sys.path`` --- no pip, no network.
    * **stage** (``force=True``): install **all** declared specs into the workspace venv
      regardless of what the launching environment happens to have.

    Args:
        vast_dir: directory of the ``.vast`` (the workspace/project root).
        specs: the ``plugins:`` list (pip requirement specs); ``None``/empty no-op.
        force: install every declared spec, not only the ones the env lacks.
        add_to_path: put the venv's ``site-packages`` on ``sys.path`` (the default). Pass
            ``False`` to **materialize only** --- used by the driver's dedicated
            plugin-install phase, which installs the packages (and streams pip's output to
            the campaign log) without importing them into the long-lived service process;
            the isolated compose subprocess does the ``sys.path`` + import later.
        position: ``"prepend"`` for a subprocess that exists to compose one project;
            ``"append"`` in the long-lived service. See :func:`_add_sys_path`.

    Returns:
        The importable ``site-packages`` path when it exists, else ``None``.

    Raises:
        RuntimeError: an install was attempted and ``pip`` failed (actionable message ---
            auth vs unreachable --- surfaced synchronously to the caller).
    """
    _reclaim_pre_venv_layout(vast_dir)
    venv_dir = _venv_dir(vast_dir)
    marker = os.path.join(plugin_dir(vast_dir), MARKER_NAME)

    if not specs:
        # A workspace that declares nothing must not put a leftover directory on the
        # shared path just because one exists: that is how one project's pins reached a
        # campaign that never asked for them. Only a directory that actually registers
        # robovast plugins is worth adding, and that is a metadata read, not an import.
        site_dir = _purelib_of(venv_dir)
        if _registers_plugins(site_dir):
            if add_to_path:
                _add_sys_path(site_dir, position)
            return site_dir
        return None

    want = _spec_hash(specs)
    marker_ok = False
    if os.path.isfile(marker):
        try:
            marker_ok = open(marker, encoding="utf-8").read().strip() == want
        except OSError:
            marker_ok = False

    if force:
        # Do not trust a marker a compose-mode (partial) pass may have written.
        to_install = list(specs)
    elif marker_ok:
        to_install = []  # already installed for this spec set and this environment
    else:
        # Detect plugins already installed (manually) and fetch only what's missing.
        to_install = [s for s in specs if not _is_importable(s)]
        detected = [s for s in specs if s not in to_install]
        if detected:
            logger.info("Using already-installed variation plugin(s): %s",
                        ", ".join(detected))
        if not to_install:
            return None  # all satisfied by the environment; no workspace venv needed

    if to_install:
        # A freshly-installed plugin that is *also* already loaded in this
        # (long-lived, multi-workspace) process cannot replace it --- warn.
        _warn_if_already_loaded(to_install)
        logger.info("Installing %d plugin(s) into %s: %s",
                    len(to_install), venv_dir, ", ".join(to_install))
        _ensure_venv(venv_dir)
        _install_into(venv_dir, _resolve_local_specs(vast_dir, to_install))
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(want)
        _warn_host_dependencies(vast_dir)

    if not os.path.isdir(venv_dir):
        return None
    site_dir = _purelib_of(venv_dir)
    if add_to_path:
        _add_sys_path(site_dir, position)
    return site_dir


def _plugin_specs_from_vast(vast_path: str) -> list:
    """The ``plugins:`` list (pip requirement specs) from a ``.vast``, or ``[]``.

    Read straight from YAML — not the full validated config — so it also works on an
    override revision and never fails on an unrelated schema issue.
    """
    import yaml  # noqa: PLC0415
    try:
        with open(vast_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return []
    specs = data.get("plugins")
    if not isinstance(specs, list):
        return []
    return [s for s in specs if isinstance(s, str) and s.strip()]


def ensure_plugins_importable(install_dir: str, vast_path: str | None = None) -> None:
    """Make a campaign's ``plugins:`` importable **in this process**.

    Postprocessing plugins and search extractors resolve off ``sys.path`` exactly like
    variation plugins (an entry-point name, or the third-party deps a local
    ``./file.py:Class`` plugin imports). The compose worker only leads ``sys.path`` with
    ``.robovast_plugins/`` inside its *subprocess*, and the controller's plugin-install
    phase is deliberately materialize-only, so anything resolving a plugin **in** the
    long-lived process — a batch analysis run, a search's per-batch
    ``search.postprocessing`` step, a search **extractor**, or a re-run in a fresh
    process / fetched campaign — would not otherwise see them. This installs the
    recorded specs into the workspace venv when absent and puts it on ``sys.path``;
    "install if absent" is what lets a re-run after a service restart resolve them.

    **Appends** rather than leads that path. Every caller here runs in the long-lived
    service, where ``sys.path`` is process-global and shared by concurrently-running
    campaigns --- see :func:`_add_sys_path`.

    Both in-process consumers must call this. A search extractor did not, which made
    ``plugins:`` silently useless for one: a local ``./search/x.py:Class`` extractor
    importing a third-party reader raised ``ModuleNotFoundError`` from the service
    process no matter what the ``.vast`` declared.

    *vast_path* names the ``.vast`` whose ``plugins:`` to read; when ``None`` the sole
    ``.vast`` directly under *install_dir* is used. Best-effort: a genuinely missing
    plugin surfaces loudly when the plugin is resolved, not here.
    """
    if vast_path is None:
        import glob as _glob  # noqa: PLC0415
        vasts = sorted(_glob.glob(os.path.join(install_dir, "*.vast")))
        vast_path = vasts[0] if vasts else None
    specs = _plugin_specs_from_vast(vast_path) if vast_path else []
    try:
        ensure_workspace_plugins(install_dir, specs, position="append")
    except Exception as e:  # noqa: BLE001 - never abort the caller on plugin prep
        logger.warning("Could not prepare campaign plugins in %s: %s",
                       install_dir, e)
