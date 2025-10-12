import os
import tempfile
import subprocess
import tarfile

from .common import get_scenario_base_path
from .file_cache import FileCache

def generate_floorplan_variations(variation_files, num_variations, seed_value, output_dir, progress_update_callback):
    file_cache = FileCache()
    file_cache.set_current_data_directory(get_scenario_base_path())
        
    script_path = os.path.join("dependencies", "scenery_builder", "scenery_builder.sh")
    
    if not os.path.exists(script_path):
        progress_update_callback(f"✗ Script not found at: {script_path}")
        return None
    
    temp_base_obj = tempfile.TemporaryDirectory(prefix="floorplan_variation_", delete=False)
    temp_base = temp_base_obj.name
    progress_update_callback(f"Created temporary directory: {temp_base}")

    all_map_dirs = []

    for variation_file in variation_files:
        variation = os.path.splitext(os.path.basename(variation_file))[0]
        progress_update_callback(f"\nProcessing: {variation}")

        files_for_hash = [variation_file] #TODO: add fpm
        hash_file_name = f"{os.path.basename(variation_file)}_{str(num_variations)}_{str(seed_value)}"
        strings_for_hash = [str(num_variations), str(seed_value)]
        cached_file = file_cache.get_cached_file(files_for_hash, hash_file_name, binary=False, content=False, strings_for_hash=strings_for_hash)

        if cached_file:
            progress_update_callback(f"✓ Using cached output for {variation}")
            all_map_dirs.append(cached_file)
        else:
            # Step 1: variation
            temp_variation_output_path = os.path.join(temp_base, variation, "variants")
            os.makedirs(temp_variation_output_path, exist_ok=True)
            progress_update_callback(
                f"Step 1: Running variation for {variation}..."
            )

            cmd1 = [
                script_path,
                "variation",
                "-i",
                variation_file,
                "-o",
                temp_variation_output_path,
                "-n",
                str(num_variations),
                "-s",
                str(seed_value),
            ]
            progress_update_callback(f"Command: {' '.join(cmd1)}")
            result1 = subprocess.run(
                cmd1, capture_output=True, text=True, check=True
            )
            if result1.stdout:
                progress_update_callback(result1.stdout)

            # Step 2: transform for each *.fpm file
            fpm_files = [
                f
                for f in os.listdir(temp_variation_output_path)
                if f.endswith(".fpm")
            ]
            progress_update_callback(
                f"Found {len(fpm_files)} FPM files to transform"
            )

            for fpm_file in fpm_files:
                variant_name = os.path.splitext(fpm_file)[0]
                fpm_path = os.path.join(temp_variation_output_path, fpm_file)
                temp_transform_path = os.path.join(temp_base, variation, "json-ld", variant_name)
                os.makedirs(temp_transform_path, exist_ok=True)

                progress_update_callback(
                    f"Step 2: Transforming {variant_name}..."
                )
                cmd2 = [
                    script_path,
                    "transform",
                    "-i",
                    fpm_path,
                    "-o",
                    temp_transform_path,
                ]
                progress_update_callback(f"Command: {' '.join(cmd2)}")
                result2 = subprocess.run(
                    cmd2, capture_output=True, text=True, check=True
                )
                if result2.stdout:
                    progress_update_callback(result2.stdout)

                # Step 3: generate map
                artifacts_path = os.path.join(temp_base, variation, "artifacts")
                temp_generate_output_path = os.path.join(artifacts_path, variant_name)
                os.makedirs(temp_generate_output_path, exist_ok=True)

                progress_update_callback(
                    f"Step 3: Generating map for {variant_name}..."
                )
                cmd3 = [
                    script_path,
                    "generate",
                    "-i",
                    temp_transform_path,
                    "-o",
                    temp_generate_output_path,
                    "occ-grid",
                ]
                progress_update_callback(f"Command: {' '.join(cmd3)}")
                result3 = subprocess.run(
                    cmd3, capture_output=True, text=True, check=True
                )
                if result3.stdout:
                    progress_update_callback(result3.stdout)


            cache_target_file_name = os.path.join(get_scenario_base_path(), file_cache.get_cache_filename(hash_file_name))
            progress_update_callback(f"\nCreating tar archive {cache_target_file_name}...")
            with tarfile.open(cache_target_file_name, "w:gz") as tar:
                tar.add(artifacts_path, arcname="")

            cache_file = file_cache.save_file_to_cache(files_for_hash, hash_file_name, None, binary=True, content=False, strings_for_hash=strings_for_hash)
            all_map_dirs.append(cache_file)

    progress_update_callback(f"Preparing temporary map directory: {output_dir}")

    for map_tar in all_map_dirs:
        try:
            with tarfile.open(map_tar, "r:*") as tf:
                tf.extractall(path=output_dir)
                print(f"Extracted {map_tar} to {output_dir}")

        except Exception as exc:
            print(f"Failed to extract {map_tar}: {exc}")
            raise ValueError("Failed to extract map tar file") from exc
    return all_map_dirs
        