# Copyright (C) 2026 Frederik Pasch
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

"""CLI commands for the Hydra-based pipeline: ``vast run`` and ``vast resolve``.

``vast run`` — Compose config via Hydra, run pipeline callbacks,
dispatch to K8s or local Docker.

``vast resolve`` — Compose and resolve config, write fully-resolved
YAML files without executing.
"""

import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from omegaconf import DictConfig, OmegaConf

logger = logging.getLogger(__name__)


def _compose_config(config_dir: str, config_name: str, overrides: tuple) -> DictConfig:
    """Compose a Hydra config from directory + overrides."""
    from hydra import compose, initialize_config_dir
    from hydra.core.global_hydra import GlobalHydra

    abs_config_dir = os.path.abspath(config_dir)
    config_file = os.path.join(abs_config_dir, f"{config_name}.yaml")
    if not os.path.exists(config_file):
        raise click.ClickException(
            f"Config file not found: {config_file}\n"
            f"Use -d to specify a directory containing {config_name}.yaml, e.g.:\n"
            f"  vast run -d configs/examples/basic_nav"
        )

    # Clear any previous Hydra state
    GlobalHydra.instance().clear()

    with initialize_config_dir(config_dir=abs_config_dir, version_base=None):
        cfg = compose(config_name=config_name, overrides=list(overrides))

    # Inject config dir and file so launcher can resolve paths
    OmegaConf.update(cfg, "_config_dir", abs_config_dir, force_add=True)
    OmegaConf.update(cfg, "_config_file",
                     os.path.join(abs_config_dir, f"{config_name}.yaml"),
                     force_add=True)

    return cfg


def _run_pipeline_for_config(cfg: DictConfig, output_dir: Path):
    """Run the pipeline for a single resolved config."""
    from robovast.pipeline.executor import run_pipeline
    return run_pipeline(cfg, output_dir)


@click.command()
@click.option('--config-dir', '-d', default='.', type=click.Path(exists=True),
              help='Directory containing the Hydra config (default: current directory)')
@click.option('--config-name', '-c', default='config',
              help='Name of the config file (without .yaml extension)')
@click.option('--multirun', '-m', is_flag=True,
              help='Enable Hydra multirun (sweep mode)')
@click.option('--local', is_flag=True,
              help='Run locally via Docker instead of K8s cluster')
@click.option('--resolved', type=click.Path(exists=True),
              help='Run a pre-resolved config file (skip pipeline)')
@click.option('--detached', is_flag=True,
              help='Submit jobs and return without waiting')
@click.argument('overrides', nargs=-1, type=click.UNPROCESSED)
def run(config_dir, config_name, multirun, local, resolved, detached, overrides):
    """Run a robovast campaign.

    Composes config via Hydra, runs pipeline callbacks to generate files,
    then dispatches to K8s cluster (default) or local Docker (--local).

    \b
    Examples:
      vast run                                          # single run
      vast run -m pipeline.floorplan.seed=1,2,3         # sweep
      vast run -m hydra/sweeper=optuna                  # optimization
      vast run --local                                  # local Docker
      vast run --resolved resolved/nav-1.yaml           # pre-resolved config
      vast run -d configs/examples/basic_nav            # specific config dir
    """
    if resolved:
        _run_resolved(resolved, local, detached)
        return

    cfg = _compose_config(config_dir, config_name, overrides)

    if multirun:
        _run_multirun(cfg, config_dir, config_name, overrides, local, detached)
    else:
        _run_single(cfg, local, detached)


def _run_single(cfg: DictConfig, local: bool, detached: bool):
    """Execute a single (non-sweep) campaign."""
    metadata = OmegaConf.to_container(cfg.get("metadata", {}), resolve=True)
    campaign_name = metadata.get("name", "campaign")
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    campaign_id = f"{campaign_name}-{timestamp}"

    # Create output directory (absolute path required for Docker volume mounts)
    output_dir = (Path("results") / campaign_id).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save resolved config for reproducibility
    hydra_dir = output_dir / ".hydra"
    hydra_dir.mkdir(exist_ok=True)
    OmegaConf.save(cfg, hydra_dir / "config.yaml")

    click.echo(f"Campaign: {campaign_id}")

    # Run pipeline
    click.echo("Running pipeline callbacks...")
    pipeline_output = output_dir / "_transient"
    ctx = _run_pipeline_for_config(cfg, pipeline_output)
    click.echo(f"Pipeline complete. Scenario: {ctx.scenario_name}")

    if local:
        from robovast.hydra_plugins.local_launcher import LocalLauncher
        launcher = LocalLauncher()
        launcher.launch(cfg, ctx, output_dir)
    else:
        from robovast.hydra_plugins.k8s_launcher import K8sLauncher
        launcher = K8sLauncher(cluster_config="default")
        launcher.launch([(cfg, ctx)], campaign_id, output_dir, detached=detached)

    click.echo(f"Campaign {campaign_id} complete.")


def _run_multirun(cfg, config_dir, config_name, overrides, local, detached):
    """Execute a multirun (sweep) campaign."""
    # Parse multirun overrides to generate the parameter matrix
    # Hydra's sweeper does this natively via its multirun infrastructure.
    # For now, we implement basic grid sweep by parsing comma-separated values.
    from hydra import compose, initialize_config_dir
    from hydra.core.global_hydra import GlobalHydra
    from hydra._internal.utils import create_config_search_path
    import itertools

    # Parse sweep overrides: find params with commas
    sweep_params = {}
    fixed_overrides = []
    for override in overrides:
        if '=' in override:
            key, value = override.split('=', 1)
            if ',' in value:
                sweep_params[key] = value.split(',')
            else:
                fixed_overrides.append(override)
        else:
            fixed_overrides.append(override)

    if not sweep_params:
        click.echo("No sweep parameters found. Running single config.")
        _run_single(cfg, local, detached)
        return

    # Generate Cartesian product of sweep parameters
    keys = list(sweep_params.keys())
    values = list(sweep_params.values())
    combinations = list(itertools.product(*values))

    click.echo(f"Sweep: {' × '.join(f'{k}={len(v)}' for k, v in sweep_params.items())} "
               f"= {len(combinations)} jobs")

    metadata = OmegaConf.to_container(cfg.get("metadata", {}), resolve=True)
    campaign_name = metadata.get("name", "campaign")
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    campaign_id = f"{campaign_name}-{timestamp}"

    # Run pipeline for each combination and collect configs
    all_configs_and_contexts = []
    for combo in combinations:
        combo_overrides = list(fixed_overrides) + [
            f"{k}={v}" for k, v in zip(keys, combo)
        ]
        combo_cfg = _compose_config(config_dir, config_name, tuple(combo_overrides))

        pipeline_output = Path(tempfile.mkdtemp(prefix=f"robovast_pipeline_"))
        ctx = _run_pipeline_for_config(combo_cfg, pipeline_output)
        all_configs_and_contexts.append((combo_cfg, ctx))
        click.echo(f"  Resolved: {' '.join(f'{k}={v}' for k, v in zip(keys, combo))}")

    output_dir = (Path("results") / campaign_id).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save sweep config
    hydra_dir = output_dir / ".hydra"
    hydra_dir.mkdir(exist_ok=True)
    OmegaConf.save(cfg, hydra_dir / "config.yaml")
    with open(hydra_dir / "overrides.yaml", "w") as f:
        yaml.dump(list(overrides), f)

    if local:
        from robovast.hydra_plugins.local_launcher import LocalLauncher
        launcher = LocalLauncher()
        for combo_cfg, ctx in all_configs_and_contexts:
            launcher.launch(combo_cfg, ctx, output_dir)
    else:
        from robovast.hydra_plugins.k8s_launcher import K8sLauncher
        launcher = K8sLauncher(cluster_config="default")
        launcher.launch(all_configs_and_contexts, campaign_id, output_dir, detached=detached)

    click.echo(f"Sweep campaign {campaign_id} complete ({len(combinations)} jobs).")


def _run_resolved(resolved_path: str, local: bool, detached: bool):
    """Execute a pre-resolved config file."""
    cfg = OmegaConf.load(resolved_path)
    abs_resolved = os.path.abspath(resolved_path)
    # Ensure _config_dir is set (resolved YAMLs already carry this from `vast resolve`)
    if "_config_dir" not in cfg:
        OmegaConf.update(cfg, "_config_dir", os.path.dirname(abs_resolved), force_add=True)
    config_dir = cfg.get("_config_dir")
    # _config_file must point to a file inside _config_dir so that prepare_campaign_configs
    # can resolve scenario_file and run_files relative to the right directory.
    # Use metadata.resolved_from (the original config) when available.
    resolved_from = OmegaConf.to_container(cfg.get("metadata", {}), resolve=True).get("resolved_from")
    if resolved_from:
        config_file = resolved_from if os.path.isabs(resolved_from) \
            else os.path.join(config_dir, resolved_from)
    else:
        config_file = os.path.join(config_dir, "config.yaml")
    OmegaConf.update(cfg, "_config_file", config_file, force_add=True)

    if not cfg.get("_resolved", False):
        click.echo("Warning: Config file does not have _resolved: true marker.")

    click.echo(f"Running resolved config: {resolved_path}")

    metadata = OmegaConf.to_container(cfg.get("metadata", {}), resolve=True)
    campaign_name = metadata.get("name", "config")
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    campaign_id = f"{campaign_name}-{timestamp}"

    output_dir = (Path("results") / campaign_id).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # No pipeline needed — config is already resolved
    from robovast.pipeline.context import PipelineContext
    scenario = OmegaConf.to_container(cfg.get("scenario", {}), resolve=True)
    ctx = PipelineContext(
        scenario_params=scenario,
        scenario_name=scenario.get("name", "config"),
    )

    if local:
        from robovast.hydra_plugins.local_launcher import LocalLauncher
        launcher = LocalLauncher()
        launcher.launch(cfg, ctx, output_dir)
    else:
        from robovast.hydra_plugins.k8s_launcher import K8sLauncher
        launcher = K8sLauncher(cluster_config="default")
        launcher.launch([(cfg, ctx)], campaign_id, output_dir, detached=detached)


@click.command()
@click.option('--config-dir', '-d', default='.', type=click.Path(exists=True),
              help='Directory containing the Hydra config')
@click.option('--config-name', '-c', default='config',
              help='Name of the config file (without .yaml extension)')
@click.option('--output', '-o', default='resolved', type=click.Path(),
              help='Output directory for resolved configs (default: resolved/)')
@click.argument('overrides', nargs=-1, type=click.UNPROCESSED)
def resolve(config_dir, config_name, output, overrides):
    """Resolve configs without executing.

    Runs the pipeline callbacks to generate files and writes fully-resolved
    YAML config files. These can be inspected, edited, or executed later
    with ``vast run --resolved``.

    \b
    Examples:
      vast resolve                                       # resolve default config
      vast resolve -m pipeline.floorplan.seed=1,2,3      # resolve sweep
      vast resolve -o my_resolved/                       # custom output dir
    """
    import itertools

    # Parse sweep overrides
    sweep_params = {}
    fixed_overrides = []
    for override in overrides:
        if '=' in override:
            key, value = override.split('=', 1)
            if ',' in value:
                sweep_params[key] = value.split(',')
            else:
                fixed_overrides.append(override)
        else:
            fixed_overrides.append(override)

    if sweep_params:
        keys = list(sweep_params.keys())
        values = list(sweep_params.values())
        combinations = list(itertools.product(*values))
    else:
        combinations = [()]
        keys = []

    os.makedirs(output, exist_ok=True)
    resolved_files = []

    for i, combo in enumerate(combinations):
        combo_overrides = list(fixed_overrides) + [
            f"{k}={v}" for k, v in zip(keys, combo)
        ]
        cfg = _compose_config(config_dir, config_name, tuple(combo_overrides))

        # Run pipeline
        pipeline_output = Path(tempfile.mkdtemp(prefix="robovast_resolve_"))
        ctx = _run_pipeline_for_config(cfg, pipeline_output)

        # Build resolved config
        resolved = OmegaConf.to_container(cfg, resolve=True)
        resolved["_resolved"] = True
        resolved["scenario"] = ctx.scenario_params
        resolved["scenario"]["name"] = ctx.scenario_name
        resolved.pop("pipeline", None)  # Pipeline already executed
        resolved["metadata"] = resolved.get("metadata", {})
        resolved["metadata"]["resolved_from"] = os.path.join(config_dir, f"{config_name}.yaml")
        resolved["metadata"]["resolved_at"] = datetime.now(timezone.utc).isoformat()

        # Write resolved YAML
        scenario_name = ctx.scenario_name
        filename = f"{scenario_name}-{i}.yaml" if len(combinations) > 1 else f"{scenario_name}.yaml"
        filepath = os.path.join(output, filename)
        with open(filepath, "w") as f:
            yaml.dump(resolved, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        resolved_files.append(filepath)
        click.echo(f"  Wrote: {filepath}")

    click.echo(f"Resolved {len(resolved_files)} config(s) to {output}/")
