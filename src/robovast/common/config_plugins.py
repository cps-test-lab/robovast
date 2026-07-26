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

"""Resolve the ``.vast`` ``plugins:`` list into a per-workspace install dir.

A ``.vast`` may declare the variation-plugin packages it needs as pip requirement
specs — typically a **git URL** (``scenario_mt @ git+https://host/repo@ref``), an
**index pin** (``scenario_mt==1.2.3``), or a **workspace-relative wheel** uploaded
into the project. The variation names it uses (``SemanticGeneration`` …) resolve
through the ``robovast.variation_types`` entry points, so the package must be
importable wherever the variations are *composed*.

Composition happens in several places — the ``robovast-service`` (for validate /
preview / campaign creation), a local ``vast exec local run``, and, for a cluster
run, the controller pod. Rather than install the plugin in each of those (the
throwaway controller pod has no credentials and cannot clone a private repo), we
install **once, into the workspace**:

* :func:`ensure_workspace_plugins` installs the declared specs into
  ``<vast_dir>/.robovast_plugins/`` with ``pip install --target`` and records a
  ``.installed`` marker (a hash of the specs), then **prepends** that directory to
  ``sys.path`` so entry points resolve and the plugin's pinned dependencies win.
* The directory is a normal part of the project tree, so it is staged to the
  object store and downloaded into the controller pod with everything else. There
  the marker already matches, so the install is **skipped** and the pod merely
  imports off ``sys.path`` — no pip, no git, no credentials.

Installing to ``--target`` (not the active venv) keeps this **non-invasive**: a
local run never mutates the user's site-packages. Prepending is what lets a plugin's
pinned dependency win over a different version the host also ships (``--target``
pulls the full closure, e.g. a *forked* ``rdflib`` the plugin requires) — a mismatch
would otherwise silently break composition (a wrong ``rdflib`` mangles JSON-LD
parsing). **This is only safe because plugin composition runs in the isolated
subprocess** (``config_generation._compose_isolated``): a fresh, short-lived process
whose ``sys.path`` this rearranges — never the long-lived robovast service, which
never imports plugin code. Because that worker is short-lived and single-purpose,
the ``_warn_if_already_loaded`` first-wins caveat below is effectively moot there;
it still guards any in-process caller (e.g. the GUI editor) that opts out of
isolation.
"""

import hashlib
import importlib
import logging
import os
import subprocess  # nosec B404 - pip on trusted, config-declared specs
import sys
from importlib.metadata import PackageNotFoundError, version

logger = logging.getLogger(__name__)

#: Per-workspace directory the declared plugins are installed into (``pip
#: --target``). Lives next to the ``.vast`` so it travels with the project.
PLUGIN_DIRNAME = ".robovast_plugins"

#: Marker file (inside :data:`PLUGIN_DIRNAME`) holding the hash of the specs that
#: were installed, so a matching set is not reinstalled (offline-safe in the pod).
MARKER_NAME = ".installed"

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
    """Stable hash of the declared specs (order-independent)."""
    joined = "\n".join(sorted(specs))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()  # nosec B324 - not security


def _requirement_name(spec: str) -> str:
    """Best-effort distribution name for a pip requirement *spec*."""
    try:
        from packaging.requirements import \
            Requirement  # pylint: disable=import-outside-toplevel
        return Requirement(spec).name
    except Exception:  # pylint: disable=broad-except
        head = spec.split("@", 1)[0].strip()
        for op in ("===", "==", ">=", "<=", "~=", "!=", ">", "<"):
            if op in head:
                head = head.split(op, 1)[0]
                break
        return head.strip()


def _diagnose_pip_failure(specs, stderr: str) -> str:
    """Turn a failed ``pip install`` into an actionable, MCP-user-facing message."""
    low = (stderr or "").lower()
    auth = ("could not read username" in low or "authentication failed" in low
            or "fatal: could not read" in low or "403 forbidden" in low)
    lines = [f"Failed to install variation plugin(s) {list(specs)}."]
    if auth:
        lines.append(
            "The source needs credentials (private repository). Provide a GitHub "
            "token at 'vast exec cluster setup' so the service can authenticate, or "
            "upload a pre-built wheel into the workspace and reference it by a "
            "workspace-relative path in 'plugins:'.")
    else:
        lines.append(
            "The source could not be installed. Check that each spec is reachable "
            "and declares its own dependencies, or upload a pre-built wheel into the "
            "workspace and reference it by a workspace-relative path in 'plugins:'.")
    if stderr:
        tail = "\n".join(stderr.strip().splitlines()[-8:])
        lines.append("pip said:\n" + tail)
    return "\n".join(lines)


def _install_target(target_dir: str, specs) -> None:
    """``pip install --target`` the specs into *target_dir* (with dependencies).

    A configured GitHub token (mounted file, or the local/dev env fallback) is
    supplied to git for this one subprocess only, via a transient ``GIT_ASKPASS``
    helper in an owner-only temp dir — never in the parent environment, gitconfig,
    or a command line.
    """
    import shutil
    import tempfile
    os.makedirs(target_dir, exist_ok=True)

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
    cmd = [sys.executable, "-m", "pip", "install", "--target", target_dir,
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


def _warn_if_already_loaded(specs) -> None:
    """Warn for each declared plugin already importable from *elsewhere*.

    Called before the workspace dir is put on ``sys.path``, so a hit means the
    distribution is visible from another location (e.g. another workspace already
    served in this long-lived process). With in-process first-wins semantics the
    already-loaded copy is what runs, so surface it instead of silently diverging.
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


def _prepend_sys_path(target_dir: str) -> None:
    """Put *target_dir* **first** on ``sys.path`` and refresh import caches.

    Prepended so the workspace's pinned plugin dependencies win — a plugin may
    need a *different* version of a package the environment also ships (``rdflib``,
    ``pyld``, …), and a mismatch silently breaks it (e.g. a newer ``rdflib`` drops
    remote JSON-LD ``@context`` triples the plugin relies on). This is only safe
    because plugin imports happen in the **isolated compose subprocess** (see
    ``config_generation._compose_isolated``), never in the long-lived service
    process — so forcing the plugin's versions here cannot disturb robovast's own
    use of those packages.
    """
    if target_dir in sys.path:
        sys.path.remove(target_dir)
    sys.path.insert(0, target_dir)
    importlib.invalidate_caches()


def ensure_workspace_plugins(vast_dir: str, specs, force: bool = False,
                             add_to_path: bool = True) -> str | None:
    """Ensure the ``.vast``'s ``plugins:`` are available for composition.

    Two modes:

    * **compose** (``force=False``, the default — used at the composition
      convergence): a plugin **already installed in the environment** is used
      as-is. This is the ``vast`` CLI path: install the plugin yourself
      (``pip install`` / ``make venv``) and it is *detected*, not re-fetched. Only
      the declared specs that are *not* importable are installed into
      ``<vast_dir>/.robovast_plugins/`` (so a service whose venv lacks them still
      resolves them). A matching ``.installed`` marker (staged into the controller
      pod) short-circuits to just adjusting ``sys.path`` — no pip, no network.
    * **stage** (``force=True`` — used by ``create_campaign`` and the CLI cluster
      launcher before shipping a project to a pod): install **all** declared specs
      into ``.robovast_plugins/`` regardless of what the launching env happens to
      have, so the directory carries every plugin into the (otherwise bare) pod.

    Args:
        vast_dir: directory of the ``.vast`` (the workspace/project root).
        specs: the ``plugins:`` list (pip requirement specs); ``None``/empty no-op.
        force: materialize every spec into the workspace dir for pod staging.
        add_to_path: put ``.robovast_plugins/`` on ``sys.path`` (the default). Pass
            ``False`` to **materialize only** — used by the driver's dedicated
            plugin-install phase, which installs the packages (and streams pip's
            output to the campaign log) without importing them into the long-lived
            service process; the isolated compose subprocess does the ``sys.path`` +
            import later.

    Returns:
        The plugin directory path when it exists, else ``None``.

    Raises:
        RuntimeError: an install was attempted and ``pip`` failed (actionable
            message — auth vs unreachable — surfaced synchronously to the caller).
    """
    target_dir = os.path.join(os.path.abspath(vast_dir), PLUGIN_DIRNAME)
    marker = os.path.join(target_dir, MARKER_NAME)

    if not specs:
        if os.path.isdir(target_dir):
            if add_to_path:
                _prepend_sys_path(target_dir)
            return target_dir
        return None

    want = _spec_hash(specs)
    marker_ok = False
    if os.path.isfile(marker):
        try:
            marker_ok = open(marker, encoding="utf-8").read().strip() == want
        except OSError:
            marker_ok = False

    if force:
        # Pod staging: everything must live in the dir; do not trust a marker that
        # a compose-mode (partial) pass may have written.
        to_install = list(specs)
    elif marker_ok:
        to_install = []  # staged/offline (e.g. the controller pod) — sys.path only
    else:
        # Detect plugins already installed (manually) and fetch only what's missing.
        to_install = [s for s in specs if not _is_importable(s)]
        detected = [s for s in specs if s not in to_install]
        if detected:
            logger.info("Using already-installed variation plugin(s): %s",
                        ", ".join(detected))
        if not to_install:
            return None  # all satisfied by the environment; no workspace dir needed

    if to_install:
        # A freshly-installed plugin that is *also* already loaded in this
        # (long-lived, multi-workspace) process cannot replace it — warn.
        _warn_if_already_loaded(to_install)
        logger.info("Installing %d variation plugin(s) into %s: %s",
                    len(to_install), target_dir, ", ".join(to_install))
        _install_target(target_dir, to_install)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(want)

    if not os.path.isdir(target_dir):
        return None
    if add_to_path:
        _prepend_sys_path(target_dir)
    return target_dir


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


def ensure_postprocessing_plugins(install_dir: str, vast_path: str | None = None) -> None:
    """Make a campaign's ``plugins:`` importable for postprocessing.

    Postprocessing plugins resolve off ``sys.path`` exactly like variation plugins
    (an entry-point name, or the third-party deps a local ``./file.py:Class`` plugin
    imports). The compose worker only leads ``sys.path`` with ``.robovast_plugins/``
    inside its *subprocess*, so a later postprocessing pass — a batch analysis run, a
    search's per-batch ``search.postprocessing`` step, or a re-run in a fresh process /
    fetched campaign — would not otherwise see them. This installs the recorded specs
    into ``<install_dir>/.robovast_plugins/`` when absent and leads ``sys.path`` with
    it; "install if absent" is what lets a re-run after a service restart resolve them.

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
        ensure_workspace_plugins(install_dir, specs)
    except Exception as e:  # noqa: BLE001 - never abort postprocessing on plugin prep
        logger.warning("Could not prepare postprocessing plugins in %s: %s",
                       install_dir, e)
