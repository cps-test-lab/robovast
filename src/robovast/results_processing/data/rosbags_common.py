#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
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

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional


def write_provenance_entry(
    provenance_file_path: Optional[str],
    output_rel: str,
    sources_rel: List[str],
    plugin_name: str,
    params: Optional[dict] = None,
) -> None:
    """Append one provenance entry to a JSON file.

    Used by container scripts to record which output was produced from which
    sources. Paths should be relative to the results root (input dir).
    If provenance_file_path is None or empty, does nothing.

    Args:
        provenance_file_path: Path to the provenance JSON file (or None to skip).
        output_rel: Output path relative to results root.
        sources_rel: List of source paths relative to results root.
        plugin_name: Name of the plugin that produced the output.
        params: Optional dict of plugin parameters.
    """
    if not provenance_file_path:
        return
    entry = {
        "output": output_rel,
        "sources": list(sources_rel),
        "plugin": plugin_name,
        "params": params if params is not None else {},
    }
    parent = os.path.dirname(provenance_file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    existing: List[dict] = []
    if os.path.exists(provenance_file_path):
        try:
            with open(provenance_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                existing = data.get("entries", [])
        except (json.JSONDecodeError, OSError):
            existing = []
    existing.append(entry)
    with open(provenance_file_path, "w", encoding="utf-8") as f:
        json.dump({"entries": existing}, f, indent=2)


def gen_msg_values(msg, prefix=""):
    if isinstance(msg, list):
        for i, val in enumerate(msg):
            yield from gen_msg_values(val, f"{prefix}[{i}]")
    elif hasattr(msg, "get_fields_and_field_types"):
        for field, type_ in msg.get_fields_and_field_types().items():
            val = getattr(msg, field)
            full_field_name = prefix + "." + field if prefix else field
            if type_.startswith("sequence<"):
                for i, aval in enumerate(val):
                    yield from gen_msg_values(aval, f"{full_field_name}[{i}]")
            else:
                yield from gen_msg_values(val, full_field_name)
    else:
        yield prefix, msg


def find_rosbags(directory, bag_dir_name="rosbag2"):
    """Find all rosbag directories using parallel directory scanning (IO-bound).

    Uses a BFS with a ThreadPoolExecutor so that large result trees (e.g. 50k
    run directories on a network filesystem) are scanned concurrently rather
    than sequentially.

    Args:
        directory: Root directory to search under.
        bag_dir_name: Subdirectory name to look for (default: "rosbag2").
                      May contain a path separator, e.g. "logs/rosout_bag".

    Returns:
        Sorted list of found rosbag directory paths.
    """
    prune_top = bag_dir_name.split("/")[0]
    found: List[str] = []

    def _scan(path: str):
        """Return (bag_paths, subdirs_to_recurse) for one directory."""
        bags: List[str] = []
        subdirs: List[str] = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.name == prune_top:
                        candidate = os.path.join(path, bag_dir_name)
                        if os.path.isdir(candidate):
                            bags.append(candidate)
                        # do not recurse into bag dir
                    else:
                        subdirs.append(entry.path)
        except OSError:
            pass
        return bags, subdirs

    n_workers = min(64, (os.cpu_count() or 4) * 8)
    pending = [directory]
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        while pending:
            futures = {executor.submit(_scan, p): p for p in pending}
            pending = []
            for fut in as_completed(futures):
                bags, subdirs = fut.result()
                found.extend(bags)
                pending.extend(subdirs)

    return sorted(found)
