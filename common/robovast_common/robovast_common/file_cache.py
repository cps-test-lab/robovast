
import os
import hashlib
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
    
    def get_cached_file(self, input_files: list, file_name: str, binary: bool = False, content = True, strings_for_hash = []) -> Optional[str]:

        """Get cached HTML if it exists and is valid"""
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
        
        if cache_file and md5_file and os.path.exists(cache_file) and os.path.exists(md5_file):
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
                    except:
                        pass
            except Exception as e:
                print(f"Error reading cache file: {e}")
                # Remove invalid cache files
                try:
                    os.remove(cache_file)
                    os.remove(md5_file)
                except:
                    pass
        print("CACHE MISS (no cache):", file_name)
        return None
    
    def save_file_to_cache(self, input_files: list, file_name: str, file_content: str, binary: bool = False, content=True, strings_for_hash = []):
        """Save file to cache"""
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