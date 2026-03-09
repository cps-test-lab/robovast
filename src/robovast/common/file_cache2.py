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

"""
FileCache2: a cleaner file cache with explicit cache keys and fast hashing.

Interface A (CacheKey builder): Build a key explicitly, then use get/set.

Storage modes (both available):

  - Store content: Use when you have data in memory. Cache writes the file.
      get(key) -> hit returns content/path; miss returns None
      set(key, data, binary=...) -> write data to cache

  - Store path: Use when you write directly to disk (e.g. tar, subprocess).
      get_path(key) -> path where cache file lives
      get(key) -> hit returns content/path; miss returns None
      set_from_path(key) -> mark valid (only stores hash; file already written)

Example - Store content:
    key = CacheKey().add_file("/path/map.pgm").add("seed", 42).add("n", 100)
    cache = FileCache2("/cache/dir", "prefix_", suffix=".tar.gz")
    if cached := cache.get(key):
        return cached
    result = expensive_compute()
    cache.set(key, result)
    return result

Example - Store path:
    key = CacheKey().add_file(variation_path).add("n", n).add("seed", seed)
    cache = FileCache2("/cache/dir", "prefix_", suffix=".tar.gz")
    if cached := cache.get(key):
        return cached
    target_path = cache.get_path(key)
    with tarfile.open(target_path, "w:gz") as tar:
        tar.add(artifacts_dir, arcname="")
    cache.set_from_path(key)
    return target_path
"""

import hashlib
import json
import logging
import os
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)



def _to_jsonable(value: Any) -> Any:
    """Convert value to JSON-serializable form."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "model_dump") and callable(getattr(value, "model_dump")):
        return _to_jsonable(value.model_dump())
    return str(value)


def _serialize_value(value: Any) -> str:
    """Serialize a value for consistent hashing."""
    return json.dumps(_to_jsonable(value), sort_keys=True)


class CacheKey:
    """
    Builder for cache keys. Supports files (path + mtime + size) and variables.
    Keys are hashable and reusable.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []

    def add_file(self, path: str, base_dir: Optional[str] = None) -> "CacheKey":
        """
        Add a file or directory to the key.

        The path component used in the key is determined as follows:
        - If *base_dir* is given and *path* is under it: ``relpath(path, base_dir)``.
          This keeps the key stable even when the project is moved, and avoids
          collisions between files that share the same basename but live in different
          directories.
        - Otherwise: the full absolute path is used.

        mtime + size are always included (no content read).

        - File: path_component + mtime + size.
        - Directory: relpath-from-dir-root + mtime + size for each file inside.
        """
        path = os.path.abspath(path)
        if base_dir is not None:
            base_dir = os.path.abspath(base_dir)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Path for hashing not found: {path}")
        if os.path.isfile(path):
            stat = os.stat(path)
            if base_dir and path.startswith(base_dir + os.sep):
                path_component = os.path.relpath(path, base_dir)
            else:
                path_component = path
            self._parts.append(f"{path_component}:{stat.st_mtime}:{stat.st_size}")
        else:
            for root, dirs, files in os.walk(path, topdown=True):
                dirs.sort()
                for f in sorted(files):
                    fp = os.path.join(root, f)
                    try:
                        stat = os.stat(fp)
                        rel = os.path.relpath(fp, path)
                        self._parts.append(f"{rel}:{stat.st_mtime}:{stat.st_size}")
                    except OSError:
                        pass
        return self

    def add(self, name: str, value: Any) -> "CacheKey":
        """Add a named value (str, int, float, bool, dict, list, bytes) to the key."""
        self._parts.append(f"{name}={_serialize_value(value)}")
        return self

    def fingerprint(self) -> str:
        """Compute MD5 fingerprint of all key parts. Fast and idempotent."""
        combined = "|".join(sorted(self._parts))
        return hashlib.md5(combined.encode()).hexdigest()


# Protocol: any object with fingerprint() -> str works as a cache key
def _key_fingerprint(key: Union[CacheKey, Any]) -> str:
    """Extract fingerprint from key (CacheKey or any object with fingerprint method)."""
    if isinstance(key, CacheKey):
        return key.fingerprint()
    if hasattr(key, "fingerprint") and callable(getattr(key, "fingerprint")):
        return key.fingerprint()
    raise TypeError(f"Key must be CacheKey or have fingerprint() method, got {type(key)}")


class FileCache2:
    """
    File cache with explicit keys. Supports two storage modes:

    - Store content: cache.set(key, data) writes data to the cache file.
    - Store path: caller writes to cache.get_path(key), then cache.set_from_path(key)
      only stores the hash (for pre-written files like tars).
    """

    def __init__(
        self,
        data_directory: str,
        prefix: str,
        *,
        suffix: str = "",
    ) -> None:
        """
        Args:
            data_directory: Base directory (cache stored in data_directory/.cache).
            prefix: Prefix for cache filename (before hash).
            suffix: Suffix for cache filename (e.g. ".tar.gz", ".html").
        """
        self._data_directory = data_directory
        self._prefix = prefix
        self._suffix = suffix
        self._cache_dir = os.path.join(str(data_directory), ".cache")
        os.makedirs(self._cache_dir, exist_ok=True)
        logger.debug("Initialized FileCache2 in %s", self._cache_dir)

    def _path_for_key(self, key: Union[CacheKey, Any]) -> tuple[str, str]:
        """Return (cache_file_path, md5_file_path)."""
        fp = _key_fingerprint(key)
        base = f"{self._prefix}{fp}{self._suffix}"
        cache_path = os.path.join(self._cache_dir, base)
        md5_path = os.path.join(self._cache_dir, f"{base}_md5")
        return cache_path, md5_path

    def get_path(self, key: Union[CacheKey, Any]) -> str:
        """
        Return the path where the cache file will be stored.
        Use with set_from_path(): write your data there, then call set_from_path(key).
        """
        cache_path, _ = self._path_for_key(key)
        return cache_path

    def get(  # pylint: disable=too-many-return-statements
        self,
        key: Union[CacheKey, Any],
        *,
        content: bool = True,
        binary: bool = False,
    ) -> Optional[Union[str, bytes]]:
        """
        Retrieve cached data if valid.

        Args:
            key: CacheKey or any object with fingerprint() -> str.
            content: If True, return file content; if False, return cache file path.
            binary: If True, return bytes; if False, return str (only when content=True).

        Returns:
            Cached content (str/bytes), path (str), or None if miss.
        """
        cache_path, md5_path = self._path_for_key(key)
        current_hash = _key_fingerprint(key)

        if not os.path.exists(md5_path):
            logger.debug("CACHE MISS (md5 file missing): %s", cache_path)
            return None

        if not os.path.exists(cache_path):
            logger.debug("CACHE MISS (cache file missing): %s", cache_path)
            return None

        try:
            with open(md5_path, "r", encoding="utf-8") as f:
                stored_hash = f.read().strip()

            if stored_hash != current_hash:
                logger.debug(
                    "CACHE MISS (hash mismatch): %s (stored: %s, current: %s)",
                    cache_path, stored_hash, current_hash,
                )
                self._remove(key)
                return None

            logger.debug("CACHE HIT: %s (hash: %s)", cache_path, current_hash)
            if content:
                if binary:
                    with open(cache_path, "rb") as f:
                        return f.read()
                with open(cache_path, "r", encoding="utf-8") as f:
                    return f.read()
            return cache_path
        except Exception as e:
            logger.warning("Error reading cache: %s", e)
            self._remove(key)
            return None

    def set(
        self,
        key: Union[CacheKey, Any],
        data: Union[str, bytes],
        *,
        binary: bool = False,
    ) -> str:
        """
        Store content in cache. Use when you have the data in memory.

        Args:
            key: CacheKey or object with fingerprint().
            data: Content to write (str or bytes).
            binary: If True, write as binary.

        Returns:
            Path to the cached file.
        """
        cache_path, md5_path = self._path_for_key(key)
        fp = _key_fingerprint(key)
        try:
            if binary:
                with open(cache_path, "wb") as f:
                    f.write(data if isinstance(data, bytes) else data.encode())
            else:
                with open(cache_path, "w", encoding="utf-8") as f:
                    f.write(data if isinstance(data, str) else data.decode())
            with open(md5_path, "w", encoding="utf-8") as f:
                f.write(fp)
            logger.info("Saved %s to cache (hash: %s)", cache_path, fp)
        except Exception as e:
            logger.warning("Error saving to cache: %s", e)
            self._remove(key)
            raise
        return cache_path

    def set_from_path(self, key: Union[CacheKey, Any]) -> str:
        """
        Mark cache as valid after caller has written to the cache path.
        Use with get_path(): obtain path, write your file there, then call this.

        Example:
            path = cache.get_path(key)
            with tarfile.open(path, "w:gz") as tar:
                tar.add(dir, arcname="")
            cache.set_from_path(key)
        """
        cache_path, md5_path = self._path_for_key(key)
        fp = _key_fingerprint(key)
        if not os.path.exists(cache_path):
            raise FileNotFoundError(
                f"Cache file not found at {cache_path}. "
                "Write your data to cache.get_path(key) before calling set_from_path."
            )
        try:
            with open(md5_path, "w", encoding="utf-8") as f:
                f.write(fp)
            logger.info("Committed cache at %s (hash: %s)", cache_path, fp)
        except Exception as e:
            logger.warning("Error committing cache: %s", e)
            raise
        return cache_path

    def _remove(self, key: Union[CacheKey, Any]) -> None:
        """Remove cache files for key."""
        cache_path, md5_path = self._path_for_key(key)
        try:
            if os.path.exists(cache_path):
                os.remove(cache_path)
            if os.path.exists(md5_path):
                os.remove(md5_path)
            logger.debug("Removed cache files for %s", cache_path)
        except Exception as e:
            logger.warning("Error removing cache: %s", e)

    def remove(self, key: Union[CacheKey, Any]) -> None:
        """Remove cache entry for key."""
        self._remove(key)
        logger.info("Removed cache entry for key")

    def get_json(self, key: Union[CacheKey, Any]) -> Optional[Any]:
        """
        Retrieve cached JSON data if valid.

        Returns:
            Deserialized Python object, or None on cache miss.
        """
        raw = self.get(key, content=True, binary=False)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception as e:
            logger.warning("Cache JSON decode error, treating as miss: %s", e)
            self._remove(key)
            return None

    def set_json(self, key: Union[CacheKey, Any], data: Any) -> str:
        """
        Serialize *data* as JSON and store in cache.

        Uses :func:`_to_jsonable` to coerce any non-standard types (Pydantic
        models, dataclasses, bytes, …) so that the serialisation never fails
        on well-behaved plugin output.

        Args:
            key: CacheKey or object with fingerprint().
            data: Any Python object that can be made JSON-serialisable via
                  ``_to_jsonable``.

        Returns:
            Path to the cached file.
        """
        raw = json.dumps(_to_jsonable(data), ensure_ascii=False)
        return self.set(key, raw, binary=False)
