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

import logging
import os
import shutil
import subprocess
import tarfile

from robovast.common import FileCache

logger = logging.getLogger(__name__)

# Auxiliary container image that produces floorplan maps/meshes. Declared by
# FloorplanVariation.get_required_container(); the active execution backend runs
# our commands in it (ephemeral ``docker run`` locally, a controller-pod sidecar
# via pods/exec in the cluster). Its entrypoint is ``floorplan``.
SCENERY_BUILDER_IMAGE = "ghcr.io/secorolab/scenery_builder"
SCENERY_BUILDER_ENTRYPOINT = ["floorplan"]


def _shared_dir(path):
    """Create *path* (and parents) world-writable so an aux container running as a
    different uid than the caller can write into it, then return it."""
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o777)
    except OSError:
        pass
    return path


def _stage_input_dir(src_dir, dst_dir):
    """Copy *src_dir* into the workspace at *dst_dir*, world-writable.

    scenery_builder writes metadata sidecars (e.g. ``<model>.fpm.yaml``) back
    into the input directory, so it must be writable by the container's user —
    which may differ from ours (e.g. the sidecar's ``appuser``). ``copytree``
    also runs ``copystat`` on *dst_dir*, resetting the source dir's mode, so we
    fix up permissions *after* the copy: dirs become 0777 and files 0666."""
    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
    for root, dirs, filenames in os.walk(dst_dir):
        for name in [root] + [os.path.join(root, d) for d in dirs]:
            try:
                os.chmod(name, 0o777)
            except OSError:
                pass
        for f in filenames:
            try:
                os.chmod(os.path.join(root, f), 0o666)
            except OSError:
                pass


def get_scenery_builder_version():
    """Return the docker image digest/ID of the scenery_builder image.

    Runs ``docker inspect`` on the image and returns its first RepoDigest
    (falling back to the image ID). Used only for provenance.

    Returns:
        The version string stripped of whitespace, or ``None`` if it cannot be
        determined (e.g. docker is unavailable, as in the controller pod, or the
        image has not been pulled yet).
    """
    for fmt in ("{{index .RepoDigests 0}}", "{{.Id}}"):
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", fmt, SCENERY_BUILDER_IMAGE],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return None
        version = result.stdout.strip()
        if result.returncode == 0 and version:
            return version
    return None


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
    maps_dir = os.path.join(output_dir, floorplan_name, 'maps')
    mesh_dir = os.path.join(output_dir, floorplan_name, '3d-mesh')

    def _pick(directory, suffix, exclude_suffix=None):
        if not os.path.isdir(directory):
            return None
        candidates = sorted(
            f for f in os.listdir(directory)
            if f.endswith(suffix) and (exclude_suffix is None or not f.endswith(exclude_suffix))
        )
        return candidates[0] if candidates else None

    # scenery_builder names artefacts after the floorplan's internal model name
    # (e.g. ``rooms.yaml``), which need not match the variation folder name
    # (e.g. ``rooms_1``). Discover the actual files rather than assuming the name.
    map_yaml_name = _pick(maps_dir, '.yaml')
    mesh_stl_name = _pick(mesh_dir, '.stl', exclude_suffix='.stl.yaml')

    if not map_yaml_name:
        raise FileNotFoundError(f"Warning: Map file (*.yaml) not found in: {maps_dir}")
    if not mesh_stl_name:
        raise FileNotFoundError(f"Warning: Mesh file (*.stl) not found in: {mesh_dir}")

    map_stem = map_yaml_name[:-len('.yaml')]
    map_pgm_name = map_stem + '.pgm'
    mesh_yaml_name = mesh_stl_name + '.yaml'

    map_file_path = os.path.join(maps_dir, map_yaml_name)
    map_pgm_path = os.path.join(maps_dir, map_pgm_name)
    mesh_file_path = os.path.join(mesh_dir, mesh_stl_name)
    mesh_file_metadata_path = os.path.join(mesh_dir, mesh_yaml_name)

    if not os.path.exists(map_pgm_path):
        raise FileNotFoundError(f"Warning: Map PGM not found: {map_pgm_path}")
    if not os.path.exists(mesh_file_metadata_path):
        raise FileNotFoundError(f"Warning: Mesh metadata file not found: {mesh_file_metadata_path}")

    rel_map_yaml_path = os.path.join('maps', map_yaml_name)
    rel_map_pgm_path = os.path.join('maps', map_pgm_name)
    rel_mesh_path = os.path.join('3d-mesh', mesh_stl_name)
    rel_mesh_metadata_path = os.path.join('3d-mesh', mesh_yaml_name)

    new_config = update_config_fn(in_config, {
        map_file_parameter_name: rel_map_yaml_path,
        mesh_file_parameter_name: rel_mesh_path,
    },
        config_files=[
        (rel_map_yaml_path, map_file_path),
        (rel_map_pgm_path, map_pgm_path),
        (rel_mesh_path, mesh_file_path),
        (rel_mesh_metadata_path, mesh_file_metadata_path)
    ],
        other_values={
            '_map_file': map_file_path,
    })
    return new_config


def generate_floorplan_variations(base_path, variation_files, num_variations, seed_value, output_dir, progress_update_callback, container_runner, scenery_builder_version=None):
    if not os.path.exists(base_path):
        progress_update_callback(f"✗ Path not found: {base_path}")
        return None

    if container_runner is None:
        raise RuntimeError(
            "FloorplanVariation requires an auxiliary container runner but none "
            "was provided by the execution backend.")

    # Everything the container reads/writes must live under the shared workspace
    # so it is visible at the same path inside the container.
    temp_base = _shared_dir(os.path.join(container_runner.workspace, "floorplan_variation"))
    progress_update_callback(f"Using container workspace: {temp_base}")

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
            # Stage the whole directory containing the variation file into the
            # workspace so the container can read it at the same absolute path.
            # The .variation file references siblings (e.g. ``import "rooms.fpm"``),
            # so the entire directory must be present (the previous docker wrapper
            # bind-mounted the containing directory for the same reason).
            input_dir = os.path.join(temp_base, variation, "input")
            _stage_input_dir(os.path.dirname(variation_file_path), input_dir)
            staged_input = os.path.join(input_dir, os.path.basename(variation_file))

            # Step 1: variation
            temp_variation_output_path = _shared_dir(os.path.join(temp_base, variation, "configs"))
            progress_update_callback(
                f"Step 1: Running variation for {variation}..."
            )

            cmd1 = [
                "variation",
                "-m", staged_input,
                "-o", temp_variation_output_path,
                "-n", str(num_variations),
                "-s", str(seed_value),
            ]
            try:
                container_runner.run(cmd1, progress_update_callback)
            except subprocess.CalledProcessError as e:
                error_msg = f"Command failed with exit code {e.returncode}"
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
                temp_transform_path = _shared_dir(os.path.join(temp_base, variation, "json-ld", config_name))

                progress_update_callback(
                    f"Step 2: Transforming {config_name}..."
                )
                cmd2 = [
                    "transform",
                    "-m", fpm_path,
                    "-o", temp_transform_path,
                ]
                try:
                    container_runner.run(cmd2, progress_update_callback)
                except subprocess.CalledProcessError as e:
                    error_msg = f"Transform command failed with exit code {e.returncode}"
                    progress_update_callback(error_msg)
                    raise ValueError(f"Transform step failed: {error_msg}") from e

                # Step 3: generate map
                artifacts_path = os.path.join(temp_base, variation, "artifacts")
                temp_generate_output_path = _shared_dir(os.path.join(artifacts_path, config_name))

                progress_update_callback(
                    f"Step 3: Generating map for {config_name}..."
                )
                cmd3 = [
                    "generate",
                    "-i", temp_transform_path,
                    "-o", temp_generate_output_path,
                    "occ-grid",
                    "mesh"
                ]
                try:
                    container_runner.run(cmd3, progress_update_callback)
                except subprocess.CalledProcessError as e:
                    error_msg = f"Generate command failed with exit code {e.returncode}"
                    progress_update_callback(error_msg)
                    raise ValueError(f"Generate step failed: {error_msg}") from e

            # Copy intermediate files (FPM configs and JSON-LD) into artifacts for caching
            for fpm_file in fpm_files:
                config_name = os.path.splitext(fpm_file)[0]
                fpm_src = os.path.join(temp_variation_output_path, fpm_file)
                fpm_dst = os.path.join(artifacts_path, config_name, "fpm")
                os.makedirs(fpm_dst, exist_ok=True)
                shutil.copy2(fpm_src, os.path.join(fpm_dst, fpm_file))

                jsonld_src = os.path.join(temp_base, variation, "json-ld", config_name)
                jsonld_dst = os.path.join(artifacts_path, config_name, "json-ld")
                if os.path.isdir(jsonld_src):
                    shutil.copytree(jsonld_src, jsonld_dst, dirs_exist_ok=True)

                # Write scenery_builder version into each config subfolder so it
                # is preserved inside the cache tar and recoverable on cache hits.
                if scenery_builder_version:
                    version_file = os.path.join(artifacts_path, config_name, "scenery_builder_version.txt")
                    with open(version_file, "w", encoding="utf-8") as vf:
                        vf.write(scenery_builder_version)

            cache_target_file_name = file_cache.get_cache_filename()
            progress_update_callback(f"\nCreating tar archive {cache_target_file_name}...")
            with tarfile.open(cache_target_file_name, "w:gz") as tar:
                tar.add(artifacts_path, arcname="")

            cache_file = file_cache.save_file_to_cache(files_for_hash, None,
                                                       binary=True, content=False, strings_for_hash=strings_for_hash)
            all_map_dirs.append(cache_file)

    progress_update_callback(f"Preparing map directory: {output_dir}")

    floorplan_versions = {}
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
                for subfolder in subfolders:
                    version_path = os.path.join(output_dir, subfolder, "scenery_builder_version.txt")
                    if os.path.exists(version_path):
                        with open(version_path, encoding="utf-8") as vf:
                            floorplan_versions[subfolder] = vf.read().strip()
        except Exception as exc:
            print(f"Failed to extract {map_tar}: {exc}")
            raise ValueError("Failed to extract map tar file") from exc

    floorplan_names.sort()
    return floorplan_names, floorplan_versions


def generate_floorplan_artifacts(base_path, floorplan_files, output_dir, progress_update_callback, container_runner, scenery_builder_version=None):
    """Generate artifacts (maps and meshes) from existing floorplan files.

    Args:
        base_path: Base path for resolving relative floorplan file paths
        floorplan_files: List of floorplan (.fpm) file paths
        output_dir: Directory where artifacts will be generated
        progress_update_callback: Callback function for progress updates
        container_runner: Backend-provided handle to run scenery_builder commands
            (see robovast.common.variation.container_runner).
        scenery_builder_version: Optional version string for the scenery_builder image.
            Written into the cache tar so it survives cache hits.

    Returns:
        Tuple of (floorplan_names, versions) where floorplan_names is a list of
        subdirectory names that were generated and versions is a dict mapping
        floorplan_name to the scenery_builder version string (or None).
    """
    if not os.path.exists(base_path):
        progress_update_callback(f"✗ Path not found: {base_path}")
        return None

    if container_runner is None:
        raise RuntimeError(
            "FloorplanGeneration requires an auxiliary container runner but none "
            "was provided by the execution backend.")

    # Everything the container reads/writes must live under the shared workspace.
    temp_base = _shared_dir(os.path.join(container_runner.workspace, "floorplan_generation"))
    progress_update_callback(f"Using container workspace: {temp_base}")

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
            # Stage the whole directory containing the floorplan file into the
            # workspace so any sibling files it references are also present.
            input_dir = os.path.join(temp_base, floorplan_basename, "input")
            _stage_input_dir(os.path.dirname(floorplan_file_path), input_dir)
            staged_input = os.path.join(input_dir, os.path.basename(floorplan_file))

            # Step 1: transform floorplan to JSON-LD
            temp_transform_path = _shared_dir(os.path.join(temp_base, floorplan_basename, "json-ld"))

            progress_update_callback(f"Step 1: Transforming {floorplan_basename}...")
            cmd_transform = [
                "transform",
                "-m", staged_input,
                "-o", temp_transform_path,
            ]
            try:
                container_runner.run(cmd_transform, progress_update_callback)
            except subprocess.CalledProcessError as e:
                error_msg = f"Transform command failed with exit code {e.returncode}"
                progress_update_callback(error_msg)
                raise ValueError(f"Transform step failed: {error_msg}") from e

            # Step 2: generate artifacts (map and mesh)
            artifacts_path = os.path.join(temp_base, floorplan_basename, "artifacts")
            temp_generate_output_path = _shared_dir(os.path.join(artifacts_path, floorplan_basename))

            progress_update_callback(f"Step 2: Generating artifacts for {floorplan_basename}...")
            cmd_generate = [
                "generate",
                "-i", temp_transform_path,
                "-o", temp_generate_output_path,
                "occ-grid",
                "mesh"
            ]
            progress_update_callback("Generating floorplan. This may take a while...")
            try:
                container_runner.run(cmd_generate, progress_update_callback)
            except subprocess.CalledProcessError as e:
                error_msg = f"Generate command failed with exit code {e.returncode}"
                progress_update_callback(error_msg)
                raise ValueError(f"Generate step failed: {error_msg}") from e

            # Copy intermediate JSON-LD files into artifacts for caching
            jsonld_dst = os.path.join(artifacts_path, floorplan_basename, "json-ld")
            if os.path.isdir(temp_transform_path):
                shutil.copytree(temp_transform_path, jsonld_dst, dirs_exist_ok=True)

            # Write scenery_builder version into the artifact directory so it
            # is preserved inside the cache tar and recoverable on cache hits.
            if scenery_builder_version:
                version_file = os.path.join(artifacts_path, floorplan_basename, "scenery_builder_version.txt")
                with open(version_file, "w", encoding="utf-8") as vf:
                    vf.write(scenery_builder_version)

            # Create tar archive for caching
            cache_target_file_name = file_cache.get_cache_filename()
            progress_update_callback(f"Creating tar archive {cache_target_file_name}...")
            with tarfile.open(cache_target_file_name, "w:gz") as tar:
                tar.add(artifacts_path, arcname="")

            cache_file = file_cache.save_file_to_cache(files_for_hash, None,
                                                       binary=True, content=False, strings_for_hash=strings_for_hash)
            all_artifacts_dirs.append(cache_file)

    progress_update_callback(f"Preparing artifact directory: {output_dir}")

    floorplan_versions = {}
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
                for subfolder in subfolders:
                    version_path = os.path.join(output_dir, subfolder, "scenery_builder_version.txt")
                    if os.path.exists(version_path):
                        with open(version_path, encoding="utf-8") as vf:
                            floorplan_versions[subfolder] = vf.read().strip()
        except Exception as exc:
            progress_update_callback(f"Failed to extract {artifacts_tar}: {exc}")
            raise ValueError("Failed to extract artifacts tar file") from exc

    floorplan_names.sort()
    return floorplan_names, floorplan_versions
