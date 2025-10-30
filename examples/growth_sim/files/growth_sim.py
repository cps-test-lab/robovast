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

import argparse
import csv
from pathlib import Path
import numpy as np

class GrowthSimulator:
    """
    Simulates population growth using logistic growth model with noise.

    Parameters:
    - initial_population: Starting population
    - growth_rate: Growth rate 0-1
    - carrying_capacity: Maximum population
    """

    def __init__(self, initial_population, growth_rate, carrying_capacity):
        self.initial_population = initial_population
        self.growth_rate = growth_rate
        self.carrying_capacity = carrying_capacity

    def run(self, time_steps=50, noise_level=0.05, ):
        """
        Run the simulation.

        Args:
            time_steps: Number of time steps to simulate
            noise_level: Standard deviation of random noise (0-1)

        Returns:
            dict with simulation results
        """
        population = np.zeros(time_steps)
        population[0] = self.initial_population

        # Logistic growth with noise
        for t in range(1, time_steps):
            # Logistic growth formula
            growth = self.growth_rate * population[t-1] * (
                1 - population[t-1] / self.carrying_capacity
            )

            # Add noise (percentage of current population)
            noise = np.random.normal(0, noise_level * population[t-1])

            population[t] = max(0, population[t-1] + growth + noise)

        # Calculate some summary statistics
        sim_results = {
            'time': list(range(time_steps)),
            'population': population.tolist(),
            'statistics': {
                'max_population': float(np.max(population)),
                'min_population': float(np.min(population)),
                'final_population': float(population[-1]),
                'avg_population': float(np.mean(population)),
            }
        }

        return sim_results

    def save_results(self, sim_results, output_path):
        """Save results to CSV file."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w', newline='') as f:
            writer = csv.writer(f)

            # Write time series data
            writer.writerow(['# Time Series Data'])
            writer.writerow(['time', 'population'])
            for time, pop in zip(sim_results['time'], sim_results['population']):
                writer.writerow([time, pop])

        return output_path


# Example usage
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Simulate population growth using logistic growth model')
    parser.add_argument('--initial-population', type=int,  required=True,
                        help='Starting population')
    parser.add_argument('--growth-rate', type=float,  required=True,
                        help='Growth rate 0-1')
    parser.add_argument('--carrying-capacity', type=int,  required=True,
                        help='Maximum population')
    parser.add_argument('--time-steps', type=int, default=1000,
                        help='Number of time steps to simulate')
    parser.add_argument('--noise-level', type=float, default=0.05,
                        help='Standard deviation of random noise 0-1 (default: 0.05)')
    parser.add_argument('--output', type=str, default='output.csv',
                        help='Output file path (default: output.csv)')

    args = parser.parse_args()

    sim = GrowthSimulator(
        initial_population=args.initial_population,
        growth_rate=args.growth_rate,
        carrying_capacity=args.carrying_capacity
    )

    results = sim.run(
        time_steps=args.time_steps,
        noise_level=args.noise_level
    )
    output_file = sim.save_results(results, args.output)

    print(f"Simulation complete!")
    print(f"Results saved to: {output_file}")
    print(f"Final population: {results['statistics']['final_population']:.1f}")
    print(f"Max population: {results['statistics']['max_population']:.1f}")
