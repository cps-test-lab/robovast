#!/usr/bin/env python3
import argparse
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

    return {"cpu_name": cpu_name}


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


def build_sysinfo(custom: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "platform": get_platform_info(),
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

    args = parser.parse_args()

    try:
        external = parse_external_kv(args.external)
    except ValueError as exc:
        parser.error(str(exc))

    sysinfo = build_sysinfo(external)
    write_yaml(sysinfo, args.output)


if __name__ == "__main__":
    main()
