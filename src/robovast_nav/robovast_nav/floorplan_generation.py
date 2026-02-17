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

import os
import subprocess
import tarfile
import tempfile

from robovast.common import FileCache


def _create_config_for_floorplan(
    floorplan_name, 
    output_dir, 
    in_config, 
    map_file_parameter_name, 
    mesh_file_parameter_name,
    update_config_fn
):
    """Create a configuration entry for a generated floorplan with its artifacts.
    
    Args:
        floorplan_name: Name of the floorplan (used as subdirectory name)
        output_dir: Base output directory containing floorplan artifacts
        in_config: Input configuration to update
        map_file_parameter_name: Config key for map file parameter
        mesh_file_parameter_name: Config key for mesh file parameter
        update_config_fn: Function to update config with new values
        
    Returns:
        Updated configuration dictionary
    """
    base_name = os.path.basename(floorplan_name).split('_')[0]
    map_file_path = os.path.join(output_dir, floorplan_name, 'maps', base_name + '.yaml')
    mesh_file_path = os.path.join(output_dir, floorplan_name, '3d-mesh', base_name + '.stl')
    mesh_file_metadata_path = os.path.join(output_dir, floorplan_name, '3d-mesh', base_name + '.stl.yaml')

    if not os.path.exists(map_file_path):
        raise FileNotFoundError(f"Warning: Map file not found: {map_file_path}")
    if not os.path.exists(mesh_file_path):
        raise FileNotFoundError(f"Warning: Mesh file not found: {mesh_file_path}")
    if not os.path.exists(mesh_file_metadata_path):
        raise FileNotFoundError(f"Warning: Mesh metadata file not found: {mesh_file_metadata_path}")
        
    rel_map_yaml_path = os.path.join('maps', base_name + '.yaml')
    rel_map_pgm_path = os.path.join('maps', base_name + '.pgm')
    rel_mesh_path = os.path.join('3d-mesh', base_name + '.stl')
    rel_mesh_metadata_path = os.path.join('3d-mesh', base_name + '.stl.yaml')
    
    new_config = update_config_fn(in_config, {
        map_file_parameter_name: rel_map_yaml_path,
        mesh_file_parameter_name: rel_mesh_path,
    },
        config_files=[
        (rel_map_yaml_path, map_file_path),
        (rel_map_pgm_path, os.path.join(output_dir, floorplan_name, 'maps', base_name + '.pgm')),
        (rel_mesh_path, mesh_file_path),
        (rel_mesh_metadata_path, mesh_file_metadata_path)
    ],
        other_values={
            '_map_file': map_file_path,
    })
    return new_config


def generate_floorplan_variations(base_path, variation_files, num_variations, seed_value, output_dir, progress_update_callback):
    if not os.path.exists(base_path):
        progress_update_callback(f"✗ Path not found: {base_path}")
        return None

    script_path = os.path.join("dependencies", "scenery_builder.sh")

    if not os.path.exists(script_path):
        progress_update_callback(f"✗ Script not found at: {script_path}")
        return FileNotFoundError(f"Script not found at: {script_path}")

    temp_base_obj = tempfile.TemporaryDirectory(prefix="floorplan_variation_")
    temp_base = temp_base_obj.name
    progress_update_callback(f"Created temporary directory: {temp_base}")

    all_map_dirs = []
    floorplan_names = []
    for variation_file in variation_files:
        variation_file_path = os.path.join(base_path, variation_file)
        if not os.path.exists(variation_file_path):
            raise FileNotFoundError(f"floorplan variation file {variation_file_path} not found")
        variation = os.path.splitext(os.path.basename(variation_file))[0]
        progress_update_callback(f"\nProcessing: {variation}")

        file_cache = FileCache(base_path, "floorplan_variation", [variation_file, num_variations, seed_value])
        files_for_hash = [variation_file_path]  # TODO: add fpm
        strings_for_hash = [str(num_variations), str(seed_value)]
        cached_file = file_cache.get_cached_file(files_for_hash, binary=False,
                                                 content=False, strings_for_hash=strings_for_hash)

        if cached_file:
            progress_update_callback(f"✓ Using cached output for {variation}")
            all_map_dirs.append(cached_file)
        else:
            # Step 1: variation
            temp_variation_output_path = os.path.join(temp_base, variation, "configs")
            os.makedirs(temp_variation_output_path, exist_ok=True)
            progress_update_callback(
                f"Step 1: Running variation for {variation}..."
            )

            cmd1 = [
                script_path,
                "variation",
                "-i",
                variation_file_path,
                "-o",
                temp_variation_output_path,
                "-n",
                str(num_variations),
                "-s",
                str(seed_value),
            ]
            progress_update_callback(f"Command: {' '.join(cmd1)}")
            try:
                result1 = subprocess.run(
                    cmd1, capture_output=True, text=True, check=True
                )
                if result1.stdout:
                    progress_update_callback(result1.stdout)
                if result1.stderr:
                    progress_update_callback(result1.stderr)
            except subprocess.CalledProcessError as e:
                error_msg = f"Command failed with exit code {e.returncode}"
                if e.stdout:
                    error_msg += f"\nStdout: {e.stdout}"
                if e.stderr:
                    error_msg += f"\nStderr: {e.stderr}"
                progress_update_callback(error_msg)
                raise ValueError(f"Variation step failed: {error_msg}") from e

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
                config_name = os.path.splitext(fpm_file)[0]
                fpm_path = os.path.join(temp_variation_output_path, fpm_file)
                temp_transform_path = os.path.join(temp_base, variation, "json-ld", config_name)
                os.makedirs(temp_transform_path, exist_ok=True)

                progress_update_callback(
                    f"Step 2: Transforming {config_name}..."
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
                try:
                    result2 = subprocess.run(
                        cmd2, capture_output=True, text=True, check=True
                    )
                    if result2.stdout:
                        progress_update_callback(result2.stdout)
                    if result2.stderr:
                        progress_update_callback(result2.stderr)
                except subprocess.CalledProcessError as e:
                    error_msg = f"Transform command failed with exit code {e.returncode}"
                    if e.stdout:
                        error_msg += f"\nStdout: {e.stdout}"
                    if e.stderr:
                        error_msg += f"\nStderr: {e.stderr}"
                    progress_update_callback(error_msg)
                    raise ValueError(f"Transform step failed: {error_msg}") from e

                # Step 3: generate map
                artifacts_path = os.path.join(temp_base, variation, "artifacts")
                temp_generate_output_path = os.path.join(artifacts_path, config_name)
                os.makedirs(temp_generate_output_path, exist_ok=True)

                progress_update_callback(
                    f"Step 3: Generating map for {config_name}..."
                )
                cmd3 = [
                    script_path,
                    "generate",
                    "-i",
                    temp_transform_path,
                    "-o",
                    temp_generate_output_path,
                    "occ-grid",
                    "mesh"
                ]
                progress_update_callback(f"Command: {' '.join(cmd3)}")
                try:
                    result3 = subprocess.run(
                        cmd3, capture_output=True, text=True, check=True
                    )
                    if result3.stdout:
                        progress_update_callback(result3.stdout)
                    if result3.stderr:
                        progress_update_callback(result3.stderr)
                except subprocess.CalledProcessError as e:
                    error_msg = f"Generate command failed with exit code {e.returncode}"
                    if e.stdout:
                        error_msg += f"\nStdout: {e.stdout}"
                    if e.stderr:
                        error_msg += f"\nStderr: {e.stderr}"
                    progress_update_callback(error_msg)
                    raise ValueError(f"Generate step failed: {error_msg}") from e

            cache_target_file_name = file_cache.get_cache_filename()
            progress_update_callback(f"\nCreating tar archive {cache_target_file_name}...")
            with tarfile.open(cache_target_file_name, "w:gz") as tar:
                tar.add(artifacts_path, arcname="")

            cache_file = file_cache.save_file_to_cache(files_for_hash, None,
                                                       binary=True, content=False, strings_for_hash=strings_for_hash)
            all_map_dirs.append(cache_file)

    progress_update_callback(f"Preparing map directory: {output_dir}")

    for map_tar in all_map_dirs:
        try:
            with tarfile.open(map_tar, "r:*") as tf:
                tf.extractall(path=output_dir)
                print(f"Extracted {map_tar} to {output_dir}")
                subfolders = [
                    m.name for m in tf.getmembers()
                    if m.isdir() and '/' not in m.name and any(
                        f.name.startswith(m.name + '/') for f in tf.getmembers() if not f.isdir()
                    )
                ]
                if not subfolders:
                    raise ValueError("No subfolders found in extracted tar file")
                floorplan_names.extend(subfolders)
        except Exception as exc:
            print(f"Failed to extract {map_tar}: {exc}")
            raise ValueError("Failed to extract map tar file") from exc

    floorplan_names.sort()
    return floorplan_names


def generate_floorplan_artifacts(base_path, floorplan_files, output_dir, progress_update_callback):
    """Generate artifacts (maps and meshes) from existing floorplan files.
    
    Args:
        base_path: Base path for resolving relative floorplan file paths
        floorplan_files: List of floorplan (.fpm) file paths
        output_dir: Directory where artifacts will be generated
        progress_update_callback: Callback function for progress updates
        
    Returns:
        List of floorplan names (subdirectory names) that were generated
    """
    if not os.path.exists(base_path):
        progress_update_callback(f"✗ Path not found: {base_path}")
        return None

    script_path = os.path.join("dependencies", "scenery_builder.sh")

    if not os.path.exists(script_path):
        progress_update_callback(f"✗ Script not found at: {script_path}")
        return FileNotFoundError(f"Script not found at: {script_path}")

    temp_base_obj = tempfile.TemporaryDirectory(prefix="floorplan_generation_")
    temp_base = temp_base_obj.name
    progress_update_callback(f"Created temporary directory: {temp_base}")

    all_artifacts_dirs = []
    floorplan_names = []
    
    for floorplan_file in floorplan_files:
        floorplan_file_path = os.path.join(base_path, floorplan_file)
        if not os.path.exists(floorplan_file_path):
            raise FileNotFoundError(f"Floorplan file {floorplan_file_path} not found")
            
        floorplan_basename = os.path.splitext(os.path.basename(floorplan_file))[0]
        progress_update_callback(f"\nProcessing: {floorplan_basename}")

        file_cache = FileCache(base_path, "floorplan_generation", [floorplan_file])
        files_for_hash = [floorplan_file_path]
        strings_for_hash = []
        cached_file = file_cache.get_cached_file(files_for_hash, binary=False,
                                                 content=False, strings_for_hash=strings_for_hash)

        if cached_file:
            progress_update_callback(f"✓ Using cached output for {floorplan_basename}")
            all_artifacts_dirs.append(cached_file)
        else:
            # Step 1: transform floorplan to JSON-LD
            temp_transform_path = os.path.join(temp_base, floorplan_basename, "json-ld")
            os.makedirs(temp_transform_path, exist_ok=True)

            progress_update_callback(f"Step 1: Transforming {floorplan_basename}...")
            cmd_transform = [
                script_path,
                "transform",
                "-i",
                floorplan_file_path,
                "-o",
                temp_transform_path,
            ]
            progress_update_callback(f"Command: {' '.join(cmd_transform)}")
            try:
                result = subprocess.run(
                    cmd_transform, capture_output=True, text=True, check=True
                )
                if result.stdout:
                    progress_update_callback(result.stdout)
                if result.stderr:
                    progress_update_callback(result.stderr)
            except subprocess.CalledProcessError as e:
                error_msg = f"Transform command failed with exit code {e.returncode}"
                if e.stdout:
                    error_msg += f"\nStdout: {e.stdout}"
                if e.stderr:
                    error_msg += f"\nStderr: {e.stderr}"
                progress_update_callback(error_msg)
                raise ValueError(f"Transform step failed: {error_msg}") from e

            # Step 2: generate artifacts (map and mesh)
            artifacts_path = os.path.join(temp_base, floorplan_basename, "artifacts")
            temp_generate_output_path = os.path.join(artifacts_path, floorplan_basename)
            os.makedirs(temp_generate_output_path, exist_ok=True)

            progress_update_callback(f"Step 2: Generating artifacts for {floorplan_basename}...")
            cmd_generate = [
                script_path,
                "generate",
                "-i",
                temp_transform_path,
                "-o",
                temp_generate_output_path,
                "occ-grid",
                "mesh"
            ]
            progress_update_callback(f"Command: {' '.join(cmd_generate)}")
            try:
                result = subprocess.run(
                    cmd_generate, capture_output=True, text=True, check=True
                )
                if result.stdout:
                    progress_update_callback(result.stdout)
                if result.stderr:
                    progress_update_callback(result.stderr)
            except subprocess.CalledProcessError as e:
                error_msg = f"Generate command failed with exit code {e.returncode}"
                if e.stdout:
                    error_msg += f"\nStdout: {e.stdout}"
                if e.stderr:
                    error_msg += f"\nStderr: {e.stderr}"
                progress_update_callback(error_msg)
                raise ValueError(f"Generate step failed: {error_msg}") from e

            # Create tar archive for caching
            cache_target_file_name = file_cache.get_cache_filename()
            progress_update_callback(f"Creating tar archive {cache_target_file_name}...")
            with tarfile.open(cache_target_file_name, "w:gz") as tar:
                tar.add(artifacts_path, arcname="")

            cache_file = file_cache.save_file_to_cache(files_for_hash, None,
                                                       binary=True, content=False, strings_for_hash=strings_for_hash)
            all_artifacts_dirs.append(cache_file)

    progress_update_callback(f"Preparing artifact directory: {output_dir}")

    for artifacts_tar in all_artifacts_dirs:
        try:
            with tarfile.open(artifacts_tar, "r:*") as tf:
                tf.extractall(path=output_dir)
                progress_update_callback(f"Extracted {artifacts_tar} to {output_dir}")
                subfolders = [
                    m.name for m in tf.getmembers()
                    if m.isdir() and '/' not in m.name and any(
                        f.name.startswith(m.name + '/') for f in tf.getmembers() if not f.isdir()
                    )
                ]
                if not subfolders:
                    raise ValueError("No subfolders found in extracted tar file")
                floorplan_names.extend(subfolders)
        except Exception as exc:
            progress_update_callback(f"Failed to extract {artifacts_tar}: {exc}")
            raise ValueError("Failed to extract artifacts tar file") from exc

    floorplan_names.sort()
    return floorplan_names
