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

"""Derived campaign inputs: files a campaign needs but does not hand-write.

A campaign's inputs are normally authored next to the ``.vast`` and frozen into
``<campaign>/_config/`` by ``execution.run_files``. Some are *derived* instead — a
navigation map compiled from a floorplan, a browser scene descriptor compiled from a
simulation world, a mesh converted from CAD. Producing those out-of-band, with a script
the user must remember to re-run, is the failure this module removes: the campaign starts
happily against a **stale or absent** artifact and only the result looks wrong.

``execution.generate`` declares them instead::

    execution:
      generate:
      - rst_scene:
          out: files/scene
          world: ../worlds/depot.yaml

Generation runs **once per composition, host-side, before ``run_files`` are collected**, so
a generated file is indistinguishable from a hand-written one everywhere downstream: it is
copied into ``<campaign>/_config/``, bind-mounted into the run at ``/config/<path>``, and
**content-hashed into the config identity** (:func:`~robovast.common.config_identifier.hash_run_files`).
That last property is the point — a campaign run against a changed world gets a different
config identifier instead of silently reusing the old one.

Two contract choices are worth knowing:

* **``out`` is a reserved key this module owns, not a plugin concept.** It is validated
  (relative, inside the project), created as a temp dir, and swapped into place only on
  success. So outputs can be expanded into ``run_files`` *without loading the plugin*, and a
  half-written artifact can never be mistaken for a finished one.
* **A generator reports what it read**, by writing a :data:`MANIFEST_NAME` file into its
  output dir. That is the staleness key for the next composition. Hand-listing inputs in the
  ``.vast`` was the alternative and it reintroduces the original bug one level up: the true
  dependency set of a compiled world includes the worlds it inherits from and their meshes,
  which no hand-written list stays in sync with.

Staleness fails towards doing the work: a generator that reports no inputs is never cached,
and a cache hit is honoured only while every recorded output is still on disk unchanged.

A generator that needs tooling this process does not have declares an auxiliary container
via :meth:`BaseInputGenerator.get_required_container`, exactly as a variation plugin does
(:meth:`robovast.common.variation.base_variation.Variation.get_required_container`) — the
active backend satisfies it with an ephemeral ``docker run`` locally or a controller-pod
sidecar in-cluster. This is what keeps the *service's* environment out of the question: the
generator runs where its tools are.
"""

import inspect
import json
import logging
import os
import shutil
import tempfile
import time
from importlib.metadata import entry_points

from robovast.common.errors import CampaignConfigError
from robovast.common.plugin_ref import is_file_ref, load_ref

logger = logging.getLogger(__name__)

#: Entry-point group for packaged input generators. Mirrors
#: ``robovast.postprocessing_commands`` — same loader shape, opposite end of the campaign.
INPUT_GENERATORS_GROUP = "robovast.input_generators"

#: Written by a generator into its output dir to declare the files it read. JSON:
#: ``{"inputs": ["<abs or vast-relative path>", ...]}``. Absent = "cannot tell",
#: which disables caching for that generator rather than risking a stale artifact.
MANIFEST_NAME = ".generated.json"


class BaseInputGenerator:
    """Produce a derived campaign input before the campaign is composed.

    Subclasses implement :meth:`__call__` and, when they need tooling this process does
    not have, override :meth:`get_required_container`.

    Instance attributes set by the runner before :meth:`__call__` (mirroring
    :class:`~robovast.common.variation.base_variation.Variation`):

    * ``container_runner`` — handle to the auxiliary container this generator declared,
      or ``None``. Its ``workspace`` is a directory visible at the same absolute path
      inside and outside the container.
    * ``progress_update`` — callable for user-visible progress lines.
    """

    #: Bumped by the plugin when its OUTPUT FORMAT changes, so an upgraded generator
    #: regenerates even though none of its inputs moved.
    FORMAT_VERSION = 1

    def __init__(self):
        self.container_runner = None
        self.progress_update = logger.info

    @classmethod
    def get_required_container(cls, parameters):
        """Auxiliary container this generator needs, or ``None`` to run in-process.

        Return a :class:`~robovast.common.variation.container_runner.ContainerSpec`.
        The declaration is backend-agnostic: locally an ephemeral ``docker run``,
        in-cluster a sidecar on the controller pod.

        Args:
            parameters: The raw (unvalidated) parameter dict for this entry, so the
                image can depend on config (e.g. a pinned version).
        """
        return None

    def __call__(self, vast_dir, out_dir, **params):
        """Write the artifact into *out_dir*.

        *out_dir* is a temporary directory the runner swaps into place on success, so a
        failure leaves the previous artifact untouched rather than half-overwritten.

        Declare what was read by writing :data:`MANIFEST_NAME` into *out_dir* — use
        :func:`write_manifest`. Without it the artifact is regenerated every composition.

        Args:
            vast_dir: Absolute path of the ``.vast`` file's directory; every relative
                path in *params* resolves against it.
            out_dir: Absolute path of the temp directory to write into.
            **params: The entry's parameters from the ``.vast``, minus ``out``.

        Returns:
            ``(success, message)``. Raise for anything the user must fix — a returned
            ``False`` and a raised error are reported identically, so prefer whichever
            carries the better message.
        """
        raise NotImplementedError


def write_manifest(out_dir, inputs):
    """Record the files a generator read, for the next composition's staleness check.

    Call this at the end of :meth:`BaseInputGenerator.__call__`. *inputs* is any iterable
    of paths; non-existent entries are dropped (a generator may list an optional input it
    did not find, and that absence is not itself a dependency).
    """
    existing = sorted({os.path.abspath(p) for p in inputs if p and os.path.isfile(p)})
    with open(os.path.join(out_dir, MANIFEST_NAME), "w", encoding="utf-8") as handle:
        json.dump({"inputs": existing}, handle, indent=2)
    return existing


def read_manifest(out_dir):
    """Inputs recorded by :func:`write_manifest`, or ``None`` if there is no manifest.

    ``None`` and ``[]`` mean different things: no manifest is "the generator cannot say
    what it read" (never cache), an empty list is "it read nothing" (also never cache,
    since there is nothing to invalidate on).
    """
    path = os.path.join(out_dir, MANIFEST_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        logger.debug("unreadable generator manifest %s: %s", path, exc)
        return None
    inputs = data.get("inputs") if isinstance(data, dict) else None
    return [str(p) for p in inputs] if isinstance(inputs, list) else None


def load_input_generators():
    """Load packaged input generators from the entry-point group, keyed by name.

    Enumeration is tolerant — one broken plugin must not make every other plugin
    invisible — but :func:`resolve_input_generator` is not: a generator a ``.vast``
    actually names is a hard error when it cannot be loaded.
    """
    plugins = {}
    try:
        eps = entry_points(group=INPUT_GENERATORS_GROUP)
    except Exception:  # noqa: BLE001 - enumeration must never break composition
        logger.debug("could not enumerate %s", INPUT_GENERATORS_GROUP)
        return plugins
    for entry in eps:
        try:
            obj = entry.load()
        except Exception as exc:  # noqa: BLE001 - reported, not fatal (see docstring)
            logger.warning("Failed to load input generator '%s': %s", entry.name, exc)
            continue
        if not inspect.isclass(obj):
            logger.warning(
                "Input generator '%s' is not a class and will be skipped; generators must "
                "be classes inheriting from BaseInputGenerator.", entry.name)
            continue
        plugins[entry.name] = obj
    return plugins


def resolve_input_generator(name, vast_dir, plugins=None):
    """Resolve a generator by entry-point name or ``<path>.py:<Class>`` file reference.

    The error names the *environment*, because the process that composes a campaign is
    not always the one the user is typing in: ``vast serve`` composes in its own
    virtualenv, so a generator installed in the caller's shell can be genuinely absent
    here. Saying "unknown plugin" without saying "unknown *where*" sends people to
    reinstall into the wrong place.
    """
    if is_file_ref(name):
        return load_ref(name, INPUT_GENERATORS_GROUP, vast_dir)
    if plugins is None:
        plugins = load_input_generators()
    if name not in plugins:
        import sys  # pylint: disable=import-outside-toplevel
        available = ", ".join(sorted(plugins)) or "(none registered)"
        raise CampaignConfigError(
            f"Unknown input generator '{name}' in execution.generate.\n"
            f"  Available in this environment: {available}\n"
            f"  Environment: {sys.prefix}\n"
            f"    (this is the process that composes the campaign — for a running "
            f"service that is 'vast serve', not your shell)\n"
            f"Install the package providing it into THAT environment, restart the "
            f"service from one that has it, or use a './path.py:Class' reference "
            f"next to the .vast.")
    return plugins[name]


def parse_generate_entry(entry, index):
    """Split one ``execution.generate`` entry into ``(name, params)``.

    Accepts the same two shapes ``results_processing.postprocessing`` does: a bare string
    (no parameters) or a single-key mapping whose value is the parameter dict.
    """
    field = f"execution.generate[{index}]"
    if isinstance(entry, str):
        return entry, {}
    if not isinstance(entry, dict) or len(entry) != 1:
        raise CampaignConfigError(
            f"{field} must be a generator name or a single-key mapping "
            f"'<name>: {{params}}', got: {entry!r}")
    name, params = next(iter(entry.items()))
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise CampaignConfigError(
            f"{field}.{name} parameters must be a mapping, got "
            f"{type(params).__name__}")
    return str(name), dict(params)


def resolve_out_dir(out, vast_dir, field):
    """Validate a generator's ``out`` and return its absolute path.

    ``out`` is owned by this module rather than by the plugin so that the outputs can be
    expanded into ``run_files`` without importing the plugin, and so that escaping the
    project is refused in one place.
    """
    from robovast.common.config_generation import \
        _validate_relative_path  # pylint: disable=import-outside-toplevel

    if not out or not isinstance(out, str):
        raise CampaignConfigError(
            f"{field} must declare 'out' — the directory, relative to the .vast, "
            f"that this generator writes into.")
    try:
        _validate_relative_path(out, field + ".out")
    except ValueError as exc:
        # Re-raised as a config error so every way of getting `out` wrong reports the same
        # way: a user mistake in the .vast, printed without a traceback.
        raise CampaignConfigError(str(exc)) from exc
    return os.path.abspath(os.path.join(vast_dir, out))


def collect_output_files(out_dir, vast_dir):
    """Generated files below *out_dir*, as paths relative to *vast_dir*.

    The manifest is excluded: it is robovast's bookkeeping, not a campaign input, and
    shipping it into ``_config/`` would put a host-absolute path list into the campaign.
    """
    found = []
    for root, _dirs, names in os.walk(out_dir):
        for name in sorted(names):
            if name == MANIFEST_NAME:
                continue
            found.append(os.path.relpath(os.path.join(root, name), vast_dir))
    return sorted(found)


def _output_fingerprint(out_dir, vast_dir):
    """``{relpath: (size, mtime_ns)}`` for the generated files — the cache-hit guard.

    Validated by size and mtime rather than mere existence: a cached stamp whose artifact
    was deleted or edited by hand must regenerate, not be trusted forever.
    """
    stamp = {}
    for rel in collect_output_files(out_dir, vast_dir):
        try:
            info = os.stat(os.path.join(vast_dir, rel))
        except OSError:
            continue
        stamp[rel] = [info.st_size, info.st_mtime_ns]
    return stamp


def _cache_key(name, generator_cls, params, out, inputs, vast_dir):
    """Cache key over the generator's identity, its parameters and what it read."""
    from robovast.common.file_cache2 import \
        CacheKey  # pylint: disable=import-outside-toplevel

    key = CacheKey()
    key.add("input_generation_version", 1)
    key.add("generator", name)
    key.add("format_version", getattr(generator_cls, "FORMAT_VERSION", 1))
    key.add("params", params)
    key.add("out", out)
    for path in sorted(inputs):
        if os.path.isfile(path):
            key.add_file(path, base_dir=vast_dir)
    return key


def _hash_inputs(inputs):
    """``[{path, sha256}]`` for the provenance record, skipping unreadable entries."""
    from robovast.common.config_identifier import \
        hash_file_content  # pylint: disable=import-outside-toplevel

    recorded = []
    for path in sorted(inputs):
        if not os.path.isfile(path):
            continue
        try:
            recorded.append({"path": path, "sha256": hash_file_content(path)})
        except OSError as exc:
            logger.debug("could not hash generator input %s: %s", path, exc)
    return recorded


def run_input_generators(vast_dir, entries, progress_update_callback=None,
                         container_runner_factory=None, use_cache=True):
    """Run every ``execution.generate`` entry and return their provenance records.

    Args:
        vast_dir: The ``.vast`` file's directory; ``out`` and every relative parameter
            resolve against it.
        entries: The raw ``execution.generate`` list.
        progress_update_callback: Called with user-visible progress lines.
        container_runner_factory: ``spec -> runner`` for generators declaring an
            auxiliary container (the active execution backend's factory).
        use_cache: Skip a generator whose recorded inputs and outputs are unchanged.

    Returns:
        A list of ``{name, params, out, outputs, inputs, cached, duration}`` dicts —
        the campaign's record of how its derived inputs were made.
    """
    progress = progress_update_callback or logger.info
    if not entries:
        return []

    plugins = load_input_generators()
    records = []
    claimed = {}
    for index, entry in enumerate(entries):
        name, params = parse_generate_entry(entry, index)
        field = f"execution.generate[{index}].{name}"
        out = params.pop("out", None)
        out_dir = resolve_out_dir(out, vast_dir, field)

        # Two generators writing the same tree is last-writer-wins with no way to tell
        # which artifact survived — refuse it rather than produce a mixed directory.
        if out in claimed:
            raise CampaignConfigError(
                f"{field}.out ({out!r}) is already written by "
                f"execution.generate[{claimed[out]}]. Two generators cannot share an "
                f"output directory.")
        claimed[out] = index

        generator_cls = resolve_input_generator(name, vast_dir, plugins)
        record = _run_one(name, generator_cls, params, out, out_dir, vast_dir, field,
                          progress, container_runner_factory, use_cache)
        records.append(record)
    return records


def _run_one(name, generator_cls, params, out, out_dir, vast_dir, field, progress,
             container_runner_factory, use_cache):
    """Generate one entry, honouring the cache, and return its provenance record."""
    from robovast.common.file_cache2 import \
        FileCache2  # pylint: disable=import-outside-toplevel

    started = time.monotonic()
    stamps = FileCache2(vast_dir, "input_generation_", suffix=".json") if use_cache else None

    prior = read_manifest(out_dir) if os.path.isdir(out_dir) else None
    if stamps is not None and prior:
        key = _cache_key(name, generator_cls, params, out, prior, vast_dir)
        cached = stamps.get_json(key)
        if cached is not None and cached.get("outputs") == _output_fingerprint(out_dir, vast_dir):
            progress(f"Input generator '{name}' is up to date ({out}).")
            return {"name": name, "params": params, "out": out,
                    "outputs": collect_output_files(out_dir, vast_dir),
                    "inputs": cached.get("inputs", []), "cached": True, "duration": 0.0}

    progress(f"Running input generator '{name}' -> {out} ...")
    generator = generator_cls()
    container_runner = _make_runner(generator_cls, params, container_runner_factory, name)
    try:
        generator.container_runner = container_runner
        generator.progress_update = progress
        # Build beside the destination, not in /tmp: the swap must be an atomic-ish
        # rename on the same filesystem, and a generator's aux container may need the
        # path to be inside the project it already has mounted.
        os.makedirs(os.path.dirname(out_dir) or vast_dir, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=f".{os.path.basename(out_dir)}.",
                                   dir=os.path.dirname(out_dir) or vast_dir)
        try:
            result = generator(vast_dir, staging, **params)
            _check_result(result, name, field, out)
            produced = collect_output_files(staging, staging)
            if not produced:
                raise CampaignConfigError(
                    f"Input generator '{name}' ({field}) reported success but wrote no "
                    f"files to {out!r}. Nothing would reach the campaign, so this is the "
                    f"404-at-view-time failure it exists to prevent.")
            inputs = read_manifest(staging)
            # mkdtemp is 0700; the swapped-in directory becomes a normal project dir.
            os.chmod(staging, 0o755)
            _swap_in(staging, out_dir)
            staging = None
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
    except CampaignConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as a user-facing config error
        raise CampaignConfigError(
            f"Input generator '{name}' ({field}) failed while writing {out!r}:\n"
            f"  {type(exc).__name__}: {exc}\n"
            f"The previous contents of {out!r} were left untouched.") from exc
    finally:
        if container_runner is not None:
            container_runner.close()

    duration = round(time.monotonic() - started, 3)
    outputs = collect_output_files(out_dir, vast_dir)
    hashed = _hash_inputs(inputs or [])
    if stamps is not None and inputs:
        stamps.set_json(_cache_key(name, generator_cls, params, out, inputs, vast_dir),
                        {"outputs": _output_fingerprint(out_dir, vast_dir), "inputs": hashed})
    elif inputs is None:
        # Not a warning: plenty of generators legitimately cannot enumerate what they
        # read. Say it once at debug level so a surprising re-run is explainable.
        logger.debug("input generator '%s' wrote no %s manifest; it will run on every "
                     "composition", name, MANIFEST_NAME)
    progress(f"Input generator '{name}' wrote {len(outputs)} file(s) to {out} "
             f"in {duration}s.")
    return {"name": name, "params": params, "out": out, "outputs": outputs,
            "inputs": hashed, "cached": False, "duration": duration}


def _check_result(result, name, field, out):
    """A generator returning ``(False, msg)`` fails as loudly as one that raises."""
    if result is None:
        return
    ok, message = result if isinstance(result, tuple) else (bool(result), "")
    if not ok:
        raise CampaignConfigError(
            f"Input generator '{name}' ({field}) failed while writing {out!r}: "
            f"{message or 'no reason given'}")


def _swap_in(staging, out_dir):
    """Replace *out_dir* with *staging*, keeping the old tree until the new one lands."""
    previous = out_dir + ".previous"
    shutil.rmtree(previous, ignore_errors=True)
    if os.path.exists(out_dir):
        os.rename(out_dir, previous)
    try:
        os.rename(staging, out_dir)
    except OSError:
        if os.path.exists(previous):
            os.rename(previous, out_dir)
        raise
    shutil.rmtree(previous, ignore_errors=True)


def _make_runner(generator_cls, params, container_runner_factory, name):
    """Build the auxiliary-container runner this generator declared, if any."""
    spec = generator_cls.get_required_container(params)
    if spec is None:
        return None
    if container_runner_factory is None:
        from robovast.common.config_generation import \
            _make_container_runner  # pylint: disable=import-outside-toplevel
        return _make_container_runner(spec)
    logger.debug("input generator '%s' requires container %s", name, spec.image)
    return container_runner_factory(spec)


def stage_for_container(runner, out_dir, inputs):
    """Mirror *inputs* and an output dir into the container's shared workspace.

    An auxiliary container sees only ``runner.workspace`` (bind-mounted at the same
    absolute path on both sides), so a generator running in one cannot read a path
    elsewhere in the project nor write straight into the staging directory. Copy the
    inputs in, hand back the in-container paths, and let :func:`collect_from_container`
    bring the result back.

    Returns ``(container_out, container_inputs)``. Everything is made world-writable
    because the container may run as a different uid than we do — the same reason
    ``floorplan_generation`` does it.
    """
    workspace = runner.workspace
    container_out = os.path.join(workspace, "out")
    os.makedirs(container_out, exist_ok=True)
    _make_writable(container_out)

    staged = []
    for index, source in enumerate(inputs):
        # Namespaced by index so two inputs sharing a basename cannot collide, and so a
        # world YAML keeps its own name (tools often derive output names from it).
        holder = os.path.join(workspace, "in", str(index))
        os.makedirs(holder, exist_ok=True)
        target = os.path.join(holder, os.path.basename(source))
        if os.path.isdir(source):
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
        staged.append(target)
    for root, dirs, names in os.walk(os.path.join(workspace, "in")):
        for entry in [root] + [os.path.join(root, d) for d in dirs] + \
                     [os.path.join(root, n) for n in names]:
            _make_writable(entry)
    return container_out, staged


def collect_from_container(container_out, out_dir):
    """Copy what the container produced into the staging dir the runner will swap in."""
    if not os.path.isdir(container_out):
        return
    shutil.copytree(container_out, out_dir, dirs_exist_ok=True)


def _make_writable(path):
    try:
        os.chmod(path, 0o777 if os.path.isdir(path) else 0o666)
    except OSError:
        pass


class Shell(BaseInputGenerator):
    """Run a shell command that writes a campaign input — the generic escape hatch.

    For a generator whose tool is already a command-line program, this needs no packaging
    at all. ``{out}`` expands to the output directory and ``{inputs[i]}`` to the i-th
    declared input, both absolute::

        execution:
          generate:
          - shell:
              out: files/scene
              inputs: ["../worlds/depot.yaml"]
              command: rst-export-web --world {inputs[0]} --out {out}

    ``inputs`` is the *fallback* staleness set, used only when the command does not report
    its own. A hand-written list is the weak option — it cannot know that a world inherits
    from another world whose meshes also matter — so prefer a tool that writes the
    :data:`MANIFEST_NAME` manifest itself, and point it there::

        command: rst-export-web --world {inputs[0]} --out {out}
                 --manifest {out}/.generated.json

    Then ``inputs`` only has to name the entry point; the tool declares the rest.

    The command is expanded with :meth:`str.format`, so a literal brace must be doubled
    (``{{``/``}}``) — relevant when passing JSON or a shell parameter expansion.

    With ``image`` set, the command runs in that container instead of this process, which
    is how a generator reaches tooling the composing environment does not have. Inputs are
    then copied into the container's workspace and the result copied back, so ``{out}``
    and ``{inputs[i]}`` are valid paths inside the container rather than outside it.
    """

    FORMAT_VERSION = 1

    @classmethod
    def get_required_container(cls, parameters):
        image = (parameters or {}).get("image")
        if not image:
            return None
        from robovast.common.variation.container_runner import \
            ContainerSpec  # pylint: disable=import-outside-toplevel
        entrypoint = (parameters or {}).get("entrypoint") or []
        return ContainerSpec(image=str(image),
                             command_prefix=[str(part) for part in entrypoint])

    def __call__(self, vast_dir, out_dir, command=None, inputs=None, image=None,
                 entrypoint=None, **_ignored):
        import shlex  # pylint: disable=import-outside-toplevel
        import subprocess  # nosec B404 - the .vast is trusted input, as for postprocessing

        if not command:
            raise CampaignConfigError(
                "shell generator requires 'command' — the command line that writes "
                "into {out}.")
        resolved = [os.path.abspath(os.path.join(vast_dir, p)) for p in (inputs or [])]
        missing = [p for p in resolved if not os.path.exists(p)]
        if missing:
            from robovast.common.errors import \
                missing_input_error  # pylint: disable=import-outside-toplevel
            raise missing_input_error(
                [("execution.generate shell 'inputs'", declared, resolved_path)
                 for declared, resolved_path in zip(inputs or [], resolved)
                 if resolved_path in missing])

        if self.container_runner is not None:
            container_out, container_inputs = stage_for_container(
                self.container_runner, out_dir, resolved)
            argv = [part.format(out=container_out, inputs=container_inputs)
                    for part in shlex.split(str(command))]
            self.container_runner.run(argv, self.progress_update)
            collect_from_container(container_out, out_dir)
        else:
            argv = [part.format(out=out_dir, inputs=resolved)
                    for part in shlex.split(str(command))]
            subprocess.run(argv, check=True)  # nosec B603 - argv from the trusted .vast

        # Only fall back to the declared inputs when the command reported nothing itself.
        # A tool that knows its real dependency set (rst-export-web --manifest lists the
        # world YAML, everything it inherits from, the MJCF and its meshes) says far more
        # than the .vast's hand-written list, and overwriting it here would silently
        # downgrade the staleness check to that one line.
        if read_manifest(out_dir) is None:
            # The *project* paths, not the container's copies: those are what a later
            # composition compares against.
            write_manifest(out_dir, resolved)
        return True, f"ran {argv[0]}"
