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

    # Apply robovast defaults that every config should have.
    # Users can still override these via command-line arguments.
    default_overrides = [
        "hydra.run.dir=results/${hydra.job.name}/${now:%Y-%m-%d_%H%M%S}",
        "hydra.sweep.dir=results/${hydra.job.name}/${now:%Y-%m-%d_%H%M%S}",
        "hydra.sweep.subdir=${hydra.job.num}",
    ]
    all_overrides = default_overrides + list(overrides)

    with initialize_config_dir(config_dir=abs_config_dir, version_base=None):
        cfg = compose(config_name=config_name, overrides=all_overrides)

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
      vast run                                               # single run
      vast run --multirun pipeline.floorplan.seed=1,2,3      # sweep 3 seeds
      vast run --multirun scenario.rate=0.3 pop=1,2,3        # fix+sweep
      vast run --multirun hydra/sweeper=optuna               # optimization
      vast run --local                                       # local Docker
      vast run --resolved resolved/nav-1.yaml                # pre-resolved
      vast run -d configs/examples/basic_nav                 # specific dir
    """
    if resolved:
        _run_resolved(resolved, local, detached)
        return

    if multirun:
        _run_multirun(config_dir, config_name, overrides, local, detached)
    else:
        cfg = _compose_config(config_dir, config_name, overrides)
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


def _sweep_combo_indices(parsed_overrides, combo_idx: int) -> list[int]:
    """Return per-sweep-param indices for the given combo index.

    BasicSweeper generates combos as the row-major Cartesian product of sweep
    values in declaration order. Given combo index ``combo_idx`` and the list
    of sweep-param lengths, we decompose the index back into per-param indices.

    Example: growth_rate=0.1,0.3 × initial_population=1,2
      lengths=[2,2], combos: 0→[0,0], 1→[0,1], 2→[1,0], 3→[1,1]
    """
    lengths = [
        len(list(o.sweep_string_iterator()))
        for o in parsed_overrides
        if o.is_sweep_override()
    ]
    indices = []
    i = combo_idx
    for l in reversed(lengths):
        indices.append(i % l)
        i //= l
    return list(reversed(indices))


def _params_to_sweep_overrides(params: dict, base_cfg=None) -> list[str]:
    """Convert hydra.sweeper.params entries to BasicSweeper CLI override strings.

    choice(a, b, c) → key=a,b,c
    range(stop)     → key=0  (single value when stop==1) or key=range(0,stop)
    range(start, stop[, step]) → key=range(start,stop[,step])

    Keys absent from base_cfg are prefixed with '+' so Hydra appends them.
    """
    import re
    from omegaconf import OmegaConf
    _MISSING = object()
    overrides = []
    for key, spec in params.items():
        prefix = ""
        if base_cfg is not None and OmegaConf.select(base_cfg, key, default=_MISSING) is _MISSING:
            prefix = "+"
        spec = str(spec).strip()
        m = re.match(r'^choice\((.+)\)$', spec)
        if m:
            values = [v.strip() for v in m.group(1).split(',')]
            overrides.append(f"{prefix}{key}={','.join(values)}")
            continue
        m = re.match(r'^range\((-?\d+)(?:,\s*(-?\d+))?(?:,\s*(-?\d+))?\)$', spec)
        if m:
            args = [g for g in m.groups() if g is not None]
            if len(args) == 1:
                stop = int(args[0])
                overrides.append(f"{prefix}{key}=0" if stop <= 1 else f"{prefix}{key}=range(0,{stop})")
            elif len(args) == 2:
                overrides.append(f"{prefix}{key}=range({args[0]},{args[1]})")
            else:
                overrides.append(f"{prefix}{key}=range({args[0]},{args[1]},{args[2]})")
            continue
        overrides.append(f"{prefix}{key}={spec}")
    return overrides


def _run_multirun(config_dir, config_name, overrides, local, detached):
    """Execute a multirun (sweep) campaign."""
    from hydra._internal.core_plugins.basic_sweeper import BasicSweeper
    from hydra.core.override_parser.overrides_parser import OverridesParser

    parsed = OverridesParser.create().parse_overrides(list(overrides))
    has_cli_sweep = any(o.is_sweep_override() for o in parsed)

    if not has_cli_sweep:
        # No CLI sweep overrides — check the config YAML for sweeper type and params.
        abs_config_dir = os.path.abspath(config_dir)
        config_file = os.path.join(abs_config_dir, f"{config_name}.yaml")
        with open(config_file) as f:
            raw_cfg = yaml.safe_load(f) or {}
        sweeper_cfg = raw_cfg.get("hydra", {}).get("sweeper", {})

        if sweeper_cfg.get("params"):
            # Check whether Optuna sweeper is explicitly selected in the defaults list.
            defaults = raw_cfg.get("defaults", [])
            uses_optuna = any(
                isinstance(d, dict)
                and any("sweeper" in str(k) for k in d)
                and list(d.values())[0] == "optuna"
                for d in defaults
            )

            if uses_optuna:
                from robovast.pipeline.optuna_sweep import run_optuna_sweep
                fixed = [o.input_line for o in parsed]
                run_optuna_sweep(
                    config_dir, config_name, sweeper_cfg, fixed,
                    local, detached,
                    compose_fn=_compose_config,
                    pipeline_fn=_run_pipeline_for_config,
                )
                return

            # BasicSweeper: convert YAML params to CLI sweep overrides and fall through.
            # Compose a base config first so unknown keys (e.g. 'trial') get '+' prefix.
            fixed = [o.input_line for o in parsed]
            base_cfg = _compose_config(config_dir, config_name, tuple(fixed))
            yaml_sweeps = _params_to_sweep_overrides(sweeper_cfg["params"], base_cfg)
            overrides = tuple(fixed) + tuple(yaml_sweeps)
            parsed = OverridesParser.create().parse_overrides(list(overrides))
            has_cli_sweep = any(o.is_sweep_override() for o in parsed)

        if not has_cli_sweep:
            cfg = _compose_config(config_dir, config_name, overrides)
            click.echo("No sweep parameters found. Running single config.")
            _run_single(cfg, local, detached)
            return

    # CLI sweep overrides present — use Hydra's BasicSweeper for Cartesian product.
    # Sweep syntax: key=a,b,c  or  key=range(1,10)
    # Returns List[List[List[str]]]: outer=chunks, middle=combos, inner=override strings.
    # max_batch_size=None → single chunk containing all combos.
    combos = BasicSweeper.split_arguments(parsed, max_batch_size=None)[0]

    click.echo(f"Sweep: {len(combos)} jobs")

    # Compose base config (using only fixed overrides) to get metadata
    fixed = [o.input_line for o in parsed if not o.is_sweep_override()]
    cfg = _compose_config(config_dir, config_name, tuple(fixed))
    metadata = OmegaConf.to_container(cfg.get("metadata", {}), resolve=True)
    campaign_name = metadata.get("name", "campaign")
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    campaign_id = f"{campaign_name}-{timestamp}"

    # Run pipeline for each combination and collect configs + per-combo subdirs.
    # Subdir format: {scenario_name}-{idx_per_param0}-{idx_per_param1}-...
    all_configs_and_contexts = []
    subdirs = []
    for combo_idx, combo_overrides in enumerate(combos):
        combo_cfg = _compose_config(config_dir, config_name, tuple(combo_overrides))

        pipeline_output = Path(tempfile.mkdtemp(prefix="robovast_pipeline_"))
        ctx = _run_pipeline_for_config(combo_cfg, pipeline_output)
        all_configs_and_contexts.append((combo_cfg, ctx))

        indices = _sweep_combo_indices(parsed, combo_idx)
        subdir = f"{ctx.scenario_name}-{'-'.join(str(i) for i in indices)}"
        subdirs.append(subdir)
        click.echo(f"  Resolved: {' '.join(combo_overrides)} → {subdir}")

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
        for (combo_cfg, ctx), subdir in zip(all_configs_and_contexts, subdirs):
            launcher.launch(combo_cfg, ctx, output_dir / subdir)
    else:
        from robovast.hydra_plugins.k8s_launcher import K8sLauncher
        launcher = K8sLauncher(cluster_config="default")
        launcher.launch(all_configs_and_contexts, campaign_id, output_dir,
                        subdirs=subdirs, detached=detached)

    click.echo(f"Sweep campaign {campaign_id} complete ({len(combos)} jobs).")


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
      vast resolve                                            # resolve default config
      vast resolve pipeline.floorplan.seed=1,2,3             # resolve sweep (Hydra syntax)
      vast resolve -o my_resolved/                           # custom output dir
    """
    from hydra._internal.core_plugins.basic_sweeper import BasicSweeper
    from hydra.core.override_parser.overrides_parser import OverridesParser

    parsed = OverridesParser.create().parse_overrides(list(overrides))
    if any(o.is_sweep_override() for o in parsed):
        combos = BasicSweeper.split_arguments(parsed, max_batch_size=None)[0]
    else:
        combos = [list(overrides)]

    os.makedirs(output, exist_ok=True)
    resolved_files = []

    for i, combo_overrides in enumerate(combos):
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
        filename = f"{scenario_name}-{i}.yaml" if len(combos) > 1 else f"{scenario_name}.yaml"
        filepath = os.path.join(output, filename)
        with open(filepath, "w") as f:
            yaml.dump(resolved, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        resolved_files.append(filepath)
        click.echo(f"  Wrote: {filepath}")

    click.echo(f"Resolved {len(resolved_files)} config(s) to {output}/")
