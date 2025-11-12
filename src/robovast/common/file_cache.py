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
import os
from typing import Optional


class FileCache:
    """
    A class to manage caching of files for efficient access.
    """

    def __init__(self, data_directory: str, cache_file_prefix: str, file_name_hash_objects: list):
        """
        Initialize the file cache.
        """
        self.current_data_directory = data_directory
        hash_parts = []
        for obj in file_name_hash_objects:
            if isinstance(obj, (str, int, float, bool)):
                hash_parts.append(str(obj))
            elif isinstance(obj, (list, tuple)):
                hash_parts.append(str(sorted([str(item) for item in obj])))
            elif isinstance(obj, dict):
                # Sort dict items for consistent hashing
                sorted_items = sorted(obj.items())
                hash_parts.append(str(sorted_items))
            else:
                # Fallback for other types (like Pydantic models)
                hash_parts.append(str(obj))

        if file_name_hash_objects:
            combined = "|".join(hash_parts)
            hash_string = hashlib.md5(combined.encode()).hexdigest()
            self.cache_file = f"{cache_file_prefix}_{hash_string}"
        else:
            self.cache_file = f"{cache_file_prefix}"

        # print(f"Initialized FileCache with cache file name: {self.get_cache_filename()}")

    def get_cache_directory(self):
        if not self.current_data_directory:
            return None
        cache_dir = os.path.join(str(self.current_data_directory), ".cache")
        os.makedirs(cache_dir, exist_ok=True)
        return cache_dir

    def get_cache_filename(self) -> str:
        """Get the cache filename for given CSV files and file name"""
        if not self.current_data_directory:
            raise ValueError("Current data directory is not set.")
        if not self.cache_file:
            raise ValueError("Cache file name is not set.")
        return os.path.join(self.get_cache_directory(), self.cache_file)

    def get_cache_md5_filename(self) -> str:
        """Get the MD5 filename for given CSV files and file name"""
        if not self.current_data_directory:
            raise ValueError("Current data directory is not set.")
        if not self.cache_file:
            raise ValueError("Cache file name is not set.")
        return os.path.join(self.get_cache_directory(), f"{self.cache_file}_md5")

    def get_cached_file(self, input_files: list, binary: bool = False, content=True, strings_for_hash=None, hash_only: bool = False, debug: bool=False) -> Optional[str]:  # pylint: disable=too-many-return-statements
        """
        Retrieves a cached file if it exists and is valid based on input files and additional hash strings.

        Args:
            input_files (list): List of input file paths used to compute the cache hash.
            file_name (str): Name of the file to retrieve from cache.
            binary (bool, optional): If True, reads the cached file in binary mode. Defaults to False.
            content (bool, optional): If True, returns the file content; if False, returns the cache file path. Defaults to True.
            strings_for_hash (list, optional): Additional strings to include in the hash calculation. Defaults to None.
            hash_only (bool, optional): If True, only checks the hash without reading the file. Defaults to False.

        Returns:
            Optional[str]: The content of the cached file (as str or bytes depending on `binary`), the cache file path, or None if cache is missing or invalid.

        Notes:
            - If the cache is valid (hash matches), returns the cached file content or path.
            - If the cache is invalid (hash mismatch or error), removes the outdated cache files and returns None.
            - Prints cache hit/miss information for debugging.
        """

        if not strings_for_hash:
            strings_for_hash = []
        cache_file = self.get_cache_filename()
        md5_file = self.get_cache_md5_filename()

        # Debug prints
        # print("----")
        # print("Checking cache for:", file_name)
        # print("Current data directory:", self.current_data_directory)
        # print("Input files:", input_files)
        # print("Cache file:", cache_file)
        # print("MD5 file:", md5_file)
        # print("----")

        if not cache_file or not md5_file:
            if debug:
                print("CACHE MISS (invalid cache paths):", cache_file)
            return None

        if not os.path.exists(md5_file):
            if debug:
                print("CACHE MISS (md5 file missing):", cache_file)
            return None

        if not hash_only and not os.path.exists(cache_file):
            if debug:
                print("CACHE MISS (cache file missing):", cache_file)
            return None

        try:
            # Read stored hash
            with open(md5_file, 'r', encoding='utf-8') as f:
                stored_hash = f.read().strip()

            # Calculate current hash
            current_hash = self.create_input_files_hash(input_files, strings_for_hash)

            # Check if hashes match
            if stored_hash == current_hash:
                if debug:
                    print("CACHE HIT:", cache_file, " (hash:", current_hash + ")")
                if content:
                    if binary:
                        with open(cache_file, 'rb') as f:
                            return f.read()
                    else:
                        with open(cache_file, 'r', encoding='utf-8') as f:
                            return f.read()
                else:
                    return cache_file
            else:
                if debug:
                    print("CACHE MISS (hash mismatch):", cache_file, f"(stored: {stored_hash}, current: {current_hash})")
                # Remove outdated cache files
                try:
                    os.remove(cache_file)
                    os.remove(md5_file)
                except Exception:
                    pass
                return None
        except Exception as e:
            print(f"Error reading cache file: {e}")
            # Remove invalid cache files
            try:
                os.remove(cache_file)
                os.remove(md5_file)
            except Exception:
                pass
            if debug:
                print("CACHE MISS (no cache):", cache_file)
        return None

    def save_file_to_cache(self, input_files: list, file_content: str, binary: bool = False, content=True, strings_for_hash=None):
        """Save file to cache"""
        if not strings_for_hash:
            strings_for_hash = []
        try:
            cache_file = self.get_cache_filename()
            md5_file = self.get_cache_md5_filename()
            if cache_file and md5_file:
                # Determine if the content is binary or text
                if content:
                    if binary:
                        # Save binary content
                        with open(cache_file, 'wb') as f:
                            f.write(file_content)
                    else:
                        # Save text content
                        with open(cache_file, 'w', encoding='utf-8') as f:
                            f.write(file_content)

                # Save MD5 hash
                csv_hash = self.create_input_files_hash(input_files, strings_for_hash=strings_for_hash)
                with open(md5_file, 'w', encoding='utf-8') as f:
                    f.write(csv_hash)
                print(f"Saved {cache_file} to cache (hash: {csv_hash})...")
        except Exception as e:
            print(f"Error saving to cache: {e}")
            return None
        return cache_file

    def create_input_files_hash(self, input_files: list, strings_for_hash: list) -> str:
        """Create a simple hash based on CSV file metadata (mtime and size) and some strings"""
        hash_data = strings_for_hash.copy()
        for input_file in input_files:
            if os.path.exists(input_file):
                stat = os.stat(input_file)
                hash_data.append(f"{input_file}:{stat.st_mtime}:{stat.st_size}")
            else:
                raise FileNotFoundError(f"Input file for hashing not found: {input_file}")

        combined = "|".join(sorted(hash_data))
        return hashlib.md5(combined.encode()).hexdigest()

    def remove_cache(self):
        """Remove cache files"""
        cache_file = self.get_cache_filename()
        md5_file = self.get_cache_md5_filename()
        try:
            if os.path.exists(cache_file):
                os.remove(cache_file)
            if os.path.exists(md5_file):
                os.remove(md5_file)
            print(f"Removed cache files: {cache_file}, {md5_file}")
        except Exception as e:
            print(f"Error removing cache files: {e}")
