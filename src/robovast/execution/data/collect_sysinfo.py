#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import platform
from typing import Any, Dict, Optional


def _read_first_existing(paths) -> Optional[str]:
    for path in paths:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
        except OSError:
            continue
    return None


def get_cpu_info() -> Dict[str, Any]:
    # CPU model/name from /proc/cpuinfo (works in most Linux containers/pods)
    cpu_name: Optional[str] = None
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu_name = line.split(":", 1)[1].strip()
                    break
                if line.startswith("Hardware") and cpu_name is None:
                    # Some ARM platforms use "Hardware" for the CPU identifier
                    cpu_name = line.split(":", 1)[1].strip()
    except OSError:
        cpu_name = None

    return {"cpu_name": cpu_name, "cpu_governor": get_cpu_governor()}


def get_cpu_governor() -> Optional[str]:
    """The host's CPU frequency governor, or ``None`` when it cannot be read.

    **Recorded because a node whose clock depends on how busy it is confounds every per-node
    measurement this repository takes.** Measured on 2026-08-27, one node, one scenario,
    varying only how many jobs shared the machine:

    ====  =====  ================
    jobs  RTF    run duration
    ====  =====  ================
    1     0.28   never finished
    2     0.38   252s
    5     0.81   117s
    ====  =====  ================

    More load made each run FASTER, because an idle node downclocks -- the governor there was
    ``powersave``, with an 800 MHz floor against a 4.5 GHz ceiling. A calibration probe, which
    by design runs alone before any campaign work, therefore measures the machine in the one
    state no campaign run will meet, and on that node could not finish inside its deadline at
    all. Small pilots are affected too: two concurrent runs took 252s against a 300s timeout.

    Read from cpu0: the governor is per-policy and could in principle differ across cores, but
    a mixed setting is not a configuration anyone chooses, and reporting one value that is
    usually right beats reporting nothing. Absent in most containers unless ``/sys`` is
    mounted through, hence ``None`` rather than a guess -- and ``None`` means NOT READ, never
    "fine", which is why the campaign-level check states which it saw.
    """
    text = _read_first_existing(
        ["/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"])
    return text.strip() if text else None


#: The governor a measurement cluster should be on. Anything else makes a node's speed a
#: function of its load, which is a variable no experiment here declares or records.
WANTED_CPU_GOVERNOR = "performance"


#: Prefix on a hashed node identity, so a reader can tell one from a hostname at a glance.
NODE_LABEL_PREFIX = "node-"

#: Hex characters kept from the digest. The identity being protected is the node NAME, whose
#: entropy is far below the digest's either way, so a longer label would buy nothing.
NODE_LABEL_HEX = 12


def node_label(name: Optional[str]) -> Optional[str]:
    """A stable, non-obvious label for the node called *name*, or ``None`` for no node.

    Hashed HERE, in the container, because this is the one place the node's name exists and
    the point is that it goes no further: the file this script writes ships inside the
    campaign archive, so a name recorded raw would travel with every published dataset.

    A plain digest of the name, with no salt, so that anyone holding the right name can
    recompute the label and find it -- no mapping has to be stored, kept in step, or
    shipped alongside the data. The cost of that is inherent and worth being explicit
    about: node names are enumerable, so this defeats casual disclosure -- a reader learns
    no names and no naming scheme -- but not someone who already suspects a scheme and
    wants it confirmed. It is not a secret; it is a name that carries no information.
    """
    if not name:
        return None
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return NODE_LABEL_PREFIX + digest[:NODE_LABEL_HEX]


def parse_external_kv(pairs) -> Dict[str, Any]:
    external: Dict[str, Any] = {}
    for item in pairs or []:
        if "=" not in item:
            raise ValueError(f"Invalid external entry '{item}', expected KEY=VALUE.")
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Invalid external entry '{item}', key must be non-empty.")
        # Keep everything as string; user can decide how to interpret it.
        external[key] = value
    return external


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    # For any other type, write a quoted string
    text = str(value)
    # Use single-quoted YAML style, escape single quotes
    text = text.replace("'", "''")
    return f"'{text}'"


def write_yaml(data: Dict[str, Any], path: str) -> None:
    """
    Minimal YAML writer for nested dicts with scalar values.
    Avoids requiring PyYAML inside the container.
    """

    def write_dict(d: Dict[str, Any], indent: int, fh) -> None:
        for k, v in d.items():
            if isinstance(v, dict):
                fh.write(" " * indent + f"{k}:\n")
                write_dict(v, indent + 2, fh)
            else:
                fh.write(" " * indent + f"{k}: {_format_scalar(v)}\n")

    with open(path, "w", encoding="utf-8") as f:
        write_dict(data, 0, f)


def get_platform_info() -> Dict[str, Any]:
    """
    Collect a broad set of information from the Python `platform` module.
    """
    return {
        "platform": platform.platform(aliased=True, terse=False),
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
    }


def _nvidia_driver_version() -> Optional[str]:
    """The driver version out of ``/proc/driver/nvidia/version``, or ``None``.

    That file reads ``NVRM version: NVIDIA UNIX x86_64 Kernel Module  535.183.01  <date>``
    -- the fields are separated by runs of spaces, and the version is the second one. It is
    worth picking out rather than storing the whole line, because the driver version is what
    a rendering difference between two clusters usually comes down to.
    """
    text = _read_first_existing(["/proc/driver/nvidia/version"])
    if not text:
        return None
    fields = [f.strip() for f in text.splitlines()[0].split("  ") if f.strip()]
    return fields[1] if len(fields) > 1 else None


def get_gpu_info() -> Dict[str, Any]:
    """What GPU this run could see, read straight from the filesystem.

    Records the three facts that decide whether a trial rendered in hardware, so the
    question can be answered from a campaign's data afterwards instead of inferred from
    wall-clock:

    * ``render_node`` -- a DRI render node is what the EGL backend needs. Its absence is
      the difference between a CPU-only machine and a broken GL install.
    * ``nvidia_model`` -- the card the driver has, from ``/proc``.
    A GPU present with no render node is the signature of a container given the device
    without the ``graphics`` driver capability, and this is where that shows up in the
    data. Deliberately no imports beyond the standard library and no subprocess: this
    script runs inside the execution image, where PyYAML is already avoided for the same
    reason.

    Deliberately does **not** report ``MUJOCO_GL`` or the bound backend. This runs as its
    own process, so it sees its own environment rather than the simulator's -- the field was
    recorded as ``null`` on every run, which is worse than absent because it reads as "no
    backend" instead of "not observable from here". Which backend was actually bound is a
    property of the process that rendered; ask it there
    (``roqsim.rendering.bound_gl_backend`` / ``bound_gl_device``).
    """
    import glob

    render_nodes = sorted(os.path.basename(p) for p in glob.glob("/dev/dri/renderD*"))
    model = None
    for path in sorted(glob.glob("/proc/driver/nvidia/gpus/*/information")):
        text = _read_first_existing([path])
        if not text:
            continue
        for line in text.splitlines():
            if line.startswith("Model:"):
                model = line.split(":", 1)[1].strip()
                break
        if model:
            break
    return {
        "render_node": render_nodes[0] if render_nodes else None,
        "nvidia_present": os.path.exists("/dev/nvidiactl"),
        "nvidia_model": model,
        "nvidia_driver": _nvidia_driver_version(),
    }


def build_sysinfo(custom: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "platform": get_platform_info(),
        "gpu": get_gpu_info(),
    }
    data.update(get_cpu_info())
    # Merge custom values. Keys without "/" become top-level.
    # Keys with a single "/" (e.g. "cpu/available_cpus") are treated as
    # subsection/key and merged into a nested dict, creating subsections
    # if necessary and overwriting existing values.
    for key, value in custom.items():
        if "/" in key:
            section, subkey = key.split("/", 1)
            if not section:
                # Empty section name is not meaningful; treat as top-level key.
                data[key] = value
                continue
            section_dict = data.get(section)
            if not isinstance(section_dict, dict):
                section_dict = {}
                data[section] = section_dict
            section_dict[subkey] = value
        else:
            data[key] = value
    return data


def get_distributions() -> Dict[str, Any]:
    """Every installed distribution, with what it registers and where it came from.

    Runs HERE, in the container, because that is the only place the answer exists: the
    packages are installed in this image and in no other. A service that walked its own
    interpreter instead reported "no asset providers" for a campaign whose image had three
    private ones -- the record was written by whichever process prepared the campaign, and on a
    cluster lane that process carries no simulator at all.

    Per distribution: ``version``, the entry-point ``groups`` it contributes to, and its
    ``direct_url`` -- which for a VCS install carries the commit, and is what turns "a private
    provider" into "this private provider, at this commit". A published dataset is judged on
    exactly that difference.

    Every group of every distribution, with no notion of which ones matter: which groups make a
    provider is the simulator backend's to say (``ASSET_ENTRY_POINT_GROUPS``), so this records
    the facts and the reader filters them -- and a second simulator needs no change here.
    """
    try:
        from importlib.metadata import distributions
    except ImportError:                                   # pragma: no cover - Python < 3.8
        return {}

    out: Dict[str, Any] = {}
    for dist in distributions():
        try:
            name = ((dist.metadata or {}).get("Name") or "").strip()
        except Exception:                                 # noqa: BLE001 - broken metadata
            continue
        if not name:
            continue
        # A duplicate name means two copies on the path: keep the first, which is the one an
        # import wins with, and merge the groups so neither copy's contribution is lost.
        entry = out.setdefault(name, {"version": dist.version or "", "groups": []})
        try:
            groups = {ep.group for ep in dist.entry_points}
        except Exception:                                 # noqa: BLE001 - broken metadata
            groups = set()
        entry["groups"] = sorted(set(entry["groups"]) | groups)
        try:
            origin = dist.read_text("direct_url.json")
        except Exception:                                 # noqa: BLE001 - unreadable
            origin = None
        if origin and "direct_url" not in entry:
            try:
                entry["direct_url"] = json.loads(origin)
            except ValueError:
                pass
    return out


def write_distributions(path: str) -> None:
    """Write :func:`get_distributions` as JSON. Never raises: this records a fact about a run
    and must not become the reason one fails."""
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(get_distributions(), handle, indent=2, sort_keys=True)
    except Exception as exc:                              # noqa: BLE001 - best effort
        print(f"could not record installed distributions: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Collect basic system information (CPU, memory) and write it to sysinfo.yaml.\n"
            "Works inside Docker containers and Kubernetes pods."
        )
    )
    parser.add_argument(
        "-o",
        "--output",
        default="sysinfo.yaml",
        help="Output YAML file path (default: sysinfo.yaml)",
    )
    parser.add_argument(
        "-e",
        "--external",
        action="append",
        metavar="KEY=VALUE",
        help=(
            "Custom key/value pair to include in the output. "
            "Can be specified multiple times."
        ),
    )

    parser.add_argument(
        "--distributions",
        metavar="PATH",
        help=(
            "Also write the installed distributions (version, entry-point groups, direct URL) "
            "as JSON here. A secondary container passes ONLY this: the pod's host facts are "
            "already recorded by the main container, while the packages differ per container -- "
            "which is the whole point, since in the ROS shape the simulator (and so every asset "
            "provider) lives in a container of its own."
        ),
    )
    parser.add_argument(
        "--node-name",
        metavar="NAME",
        help=(
            "The cluster node this run landed on. Recorded ONLY as its hashed "
            "'node_label' -- the name itself is never written, because this file travels "
            "in the campaign archive. Empty or absent records no label, which is the "
            "honest answer off a cluster."
        ),
    )
    parser.add_argument(
        "--no-sysinfo",
        action="store_true",
        help="Skip the sysinfo output. For a container that only reports its distributions.",
    )

    args = parser.parse_args()

    try:
        external = parse_external_kv(args.external)
    except ValueError as exc:
        parser.error(str(exc))

    label = node_label(args.node_name)
    if label:
        external["node_label"] = label

    if not args.no_sysinfo:
        sysinfo = build_sysinfo(external)
        write_yaml(sysinfo, args.output)
    if args.distributions:
        write_distributions(args.distributions)


if __name__ == "__main__":
    main()
