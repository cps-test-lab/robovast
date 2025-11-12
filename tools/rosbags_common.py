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

import hashlib
import json
import os
import time
from pathlib import Path


def compute_rosbag_hash(bag_path):
    """Compute a hash for a rosbag based on modification time and file sizes."""
    path = Path(bag_path)

    # Collect all relevant files (mcap files and metadata.yaml)
    mcap_files = list(path.glob("*.mcap"))
    metadata_file = path / "metadata.yaml"

    files_to_check = mcap_files
    if metadata_file.exists():
        files_to_check.append(metadata_file)

    # Create hash based on file stats (even if empty)
    hash_data = []
    for file_path in sorted(files_to_check):
        stat = file_path.stat()
        hash_data.append({
            "name": file_path.name,
            "size": stat.st_size,
            "mtime": stat.st_mtime
        })

    # Create a simple hash string
    hash_string = json.dumps(hash_data, sort_keys=True)
    return hashlib.md5(hash_string.encode()).hexdigest()


def get_hash_file_path(bag_path, prefix):
    """Get the path to the hash file for a rosbag."""
    parent_folder = os.path.abspath(os.path.dirname(bag_path))
    return os.path.join(parent_folder, os.path.basename(bag_path) + '_' + prefix + '.hash')


def write_hash_file(bag_path, prefix):
    """Write the hash file after successful processing."""
    bag_hash = compute_rosbag_hash(bag_path)
    hash_file = get_hash_file_path(bag_path, prefix)
    hash_info = {
        "hash": bag_hash,
        "created_at": time.time()
    }
    with open(hash_file, 'w') as f:
        json.dump(hash_info, f, indent=2)


def should_skip_processing(bag_path, prefix):
    """Check if processing should be skipped based on hash file."""
    hash_file = get_hash_file_path(bag_path, prefix)

    if not os.path.exists(hash_file):
        return False

    try:
        with open(hash_file, 'r') as f:
            stored_info = json.load(f)

        stored_hash = stored_info.get("hash")
        current_hash = compute_rosbag_hash(bag_path)

        if stored_hash == current_hash:
            return True
    except (json.JSONDecodeError, KeyError, OSError):
        # If hash file is corrupted or unreadable, reprocess
        return False

    return False


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
    """Find all rosbag directories in subdirectories."""
    rosbag_dirs = []
    for root, _, files in os.walk(directory):
        # Check if this directory contains .mcap files or metadata.yaml (rosbag indicators)
        has_mcap = any(f.endswith('.mcap') for f in files)
        has_metadata = 'metadata.yaml' in files

        if has_mcap or has_metadata:
            rosbag_dirs.append(root)

    return rosbag_dirs
