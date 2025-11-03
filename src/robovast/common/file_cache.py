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

    def __init__(self):
        """
        Initialize the file cache.
        """
        self.current_data_directory = None

    def set_current_data_directory(self, directory_path: str):
        """Set the current data directory"""
        self.current_data_directory = directory_path

    def get_cache_directory(self):
        if not self.current_data_directory:
            return None
        cache_dir = os.path.join(str(self.current_data_directory), ".cache")
        os.makedirs(cache_dir, exist_ok=True)
        return cache_dir

    def get_cache_filename(self, file_name: str) -> str:
        """Get the cache filename for given CSV files and file name"""
        return os.path.join(self.get_cache_directory(), file_name)

    def get_cache_md5_filename(self, file_name: str) -> str:
        """Get the MD5 filename for given CSV files and file name"""
        if not self.current_data_directory:
            return None

        cache_dir = os.path.join(str(self.current_data_directory), ".cache")
        os.makedirs(cache_dir, exist_ok=True)
        return os.path.join(cache_dir, f"{file_name}_md5")

    def get_cached_file(self, input_files: list, file_name: str, binary: bool = False, content=True, strings_for_hash=None, hash_only: bool=False) -> Optional[str]: # pylint: disable=too-many-return-statements
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
        cache_file = self.get_cache_filename(file_name)
        md5_file = self.get_cache_md5_filename(file_name)

        # Debug prints
        # print("----")
        # print("Checking cache for:", file_name)
        # print("Current data directory:", self.current_data_directory)
        # print("Input files:", input_files)
        # print("Cache file:", cache_file)
        # print("MD5 file:", md5_file)
        # print("----")

        if not cache_file or not md5_file:
            print("CACHE MISS (invalid cache paths):", file_name)
            return None
        
        if not os.path.exists(md5_file):
            print("CACHE MISS (md5 file missing):", file_name)
            return None

        if not hash_only and not os.path.exists(cache_file):
            print("CACHE MISS (cache file missing):", file_name)
            return None

        try:
            # Read stored hash
            with open(md5_file, 'r', encoding='utf-8') as f:
                stored_hash = f.read().strip()

            # Calculate current hash
            current_hash = self.create_input_files_hash(input_files, strings_for_hash)

            # Check if hashes match
            if stored_hash == current_hash:
                print("CACHE HIT:", file_name, " (hash:", current_hash + ")")
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
                print("CACHE MISS (hash mismatch):", file_name, f"(stored: {stored_hash}, current: {current_hash})")
                # Remove outdated cache files
                try:
                    os.remove(cache_file)
                    os.remove(md5_file)
                except Exception:
                    pass
        except Exception as e:
            print(f"Error reading cache file: {e}")
            # Remove invalid cache files
            try:
                os.remove(cache_file)
                os.remove(md5_file)
            except Exception:
                pass
        print("CACHE MISS (no cache):", file_name)
        return None

    def save_file_to_cache(self, input_files: list, file_name: str, file_content: str, binary: bool = False, content=True, strings_for_hash=None):
        """Save file to cache"""
        if not strings_for_hash:
            strings_for_hash = []
        try:
            cache_file = self.get_cache_filename(file_name)
            md5_file = self.get_cache_md5_filename(file_name)
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
                print(f"Saved {file_name} to cache (hash: {csv_hash})...")
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

        combined = "|".join(sorted(hash_data))
        return hashlib.md5(combined.encode()).hexdigest()
