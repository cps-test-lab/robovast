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

"""CLI plugin for variation management."""

import click
import os
import sys
import tempfile

from robovast.common import generate_scenario_variations


@click.group()
def variation():
    """Manage scenario variations.
    
    Generate and list scenario variations from configuration files.
    """
    pass


@variation.command(name='list')
@click.option('--config', '-c', required=True, type=click.Path(exists=True),
              help='Path to .vast configuration file')
def list_cmd(config):
    """List scenario variants without generating files.
    
    This command shows all variants that would be generated from the
    configuration file without actually creating the output files.
    """
    def progress_callback(message):
        click.echo(message)
    
    click.echo(f"Listing scenario variants from {config}...")
    click.echo("-" * 60)

    with tempfile.TemporaryDirectory(prefix="list_variants_") as temp_dir:
        try:
            variants = generate_scenario_variations(
                variation_file=config,
                progress_update_callback=progress_callback,
                output_dir=temp_dir
            )

            if variants:
                click.echo("-" * 60)
                variants_file = os.path.join(temp_dir, "scenario.variants")
                if os.path.exists(variants_file):
                    with open(variants_file, "r", encoding="utf-8") as vf:
                        click.echo(vf.read())
                else:
                    click.echo(f"No scenario.variants file found at {variants_file}", err=True)
                    sys.exit(1)
            else:
                click.echo("✗ Failed to list scenario variants", err=True)
                sys.exit(1)

        except Exception as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)


@variation.command()
@click.option('--config', '-c', required=True, type=click.Path(exists=True),
              help='Path to .vast configuration file')
@click.option('--output', '-o', required=True, type=click.Path(),
              help='Output directory for generated scenarios variants and files')
def generate(config, output):
    """Generate scenario variants and output files.
    
    Creates all variant configurations and associated files in the
    specified output directory.
    """
    def progress_callback(message):
        click.echo(message)
    
    click.echo(f"Generating scenario variants from {config}...")
    click.echo(f"Output directory: {output}")
    click.echo("-" * 60)

    try:
        variants = generate_scenario_variations(
            variation_file=config,
            progress_update_callback=progress_callback,
            output_dir=output
        )

        if variants:
            click.echo("-" * 60)
            click.echo(f"✓ Successfully generated {len(variants)} scenario variants!")
        else:
            click.echo("✗ Failed to generate scenario variants", err=True)
            sys.exit(1)

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
