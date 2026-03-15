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

import concurrent.futures
import json
import os
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


def find_rosbags(directory):
    """Find all rosbag directories in subdirectories (parallel directory scan)."""

    def scan_dir(path):
        """Scan one directory; return (path, is_rosbag, subdirectory_paths)."""
        subdirs = []
        is_rosbag = False
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry.path)
                    elif entry.name.endswith('.mcap') or entry.name == 'metadata.yaml':
                        is_rosbag = True
        except PermissionError:
            pass
        return path, is_rosbag, subdirs

    rosbag_dirs = []
    max_workers = min(32, (os.cpu_count() or 1) * 4)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        pending = {executor.submit(scan_dir, directory)}
        while pending:
            done, pending = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                path, is_rosbag, subdirs = future.result()
                if is_rosbag:
                    rosbag_dirs.append(path)
                for subdir in subdirs:
                    pending.add(executor.submit(scan_dir, subdir))

    return rosbag_dirs
