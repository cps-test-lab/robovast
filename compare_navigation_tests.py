#!/usr/bin/env python3
"""
Statistical comparison of robot navigation test results.

This script extracts time and distance metrics from test runs, performs 
distribution fitting, and compares test types using nonparametric methods.
"""

import argparse
import csv
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import warnings

import numpy as np
import pandas as pd
from scipy import stats
from scipy.stats import (
    kstest, normaltest, shapiro, anderson, jarque_bera,
    gamma, expon, weibull_min, lognorm, norm, poisson
)
import matplotlib.pyplot as plt

warnings.filterwarnings('ignore')


def extract_pose_metrics(poses_csv_path: str) -> Optional[Tuple[float, float]]:
    """
    Extract total time and distance from a poses.csv file.
    
    Args:
        poses_csv_path: Path to the poses.csv file
        
    Returns:
        Tuple of (total_time, total_distance) or None if file is invalid
    """
    try:
        df = pd.read_csv(poses_csv_path)
        
        if len(df) < 2:
            return None
        
        # Calculate total time (difference between last and first timestamp)
        timestamps = df['timestamp'].values
        total_time = timestamps[-1] - timestamps[0]
        
        # Calculate total distance (sum of Euclidean distances)
        positions = df[['position.x', 'position.y', 'position.z']].values
        distances = np.diff(positions, axis=0)
        euclidean_distances = np.linalg.norm(distances, axis=1)
        total_distance = np.sum(euclidean_distances)
        
        return total_time, total_distance
    except Exception as e:
        print(f"Error processing {poses_csv_path}: {e}", file=sys.stderr)
        return None


def extract_localization_error_metrics(localization_error_csv_path: str) -> Optional[Tuple[float, float]]:
    """
    Extract mean and variance of localization error from a localization_error.csv file.

    Supports two formats:
    - New format: uses ``error_distance_meters`` (estimated pose vs ground truth)
    - Legacy format: computes ``sqrt(covariance.x_x + covariance.y_y)``
    
    Args:
        localization_error_csv_path: Path to the localization_error.csv file
        
    Returns:
        Tuple of (mean_error, variance_error) or None if file is invalid
    """
    try:
        df = pd.read_csv(localization_error_csv_path)
        
        if len(df) < 1:
            return None
        
        if 'error_distance_meters' in df.columns:
            metric_series = df['error_distance_meters']
        elif 'covariance.x_x' in df.columns and 'covariance.y_y' in df.columns:
            metric_series = np.sqrt(df['covariance.x_x'] + df['covariance.y_y'])
        else:
            missing_cols = [
                col for col in ['error_distance_meters', 'covariance.x_x', 'covariance.y_y']
                if col not in df.columns
            ]
            print(
                f"Error processing {localization_error_csv_path}: missing required columns ({', '.join(missing_cols)})",
                file=sys.stderr,
            )
            return None

        # Calculate mean and variance of localization metric
        mean_err = float(metric_series.mean())
        var_err = float(metric_series.var())
        
        return mean_err, var_err
    except Exception as e:
        print(f"Error processing {localization_error_csv_path}: {e}", file=sys.stderr)
        return None


def extract_poses_from_scenario_config(scenario_config_path: str) -> Optional[Tuple[Tuple[float, float], List[Tuple[float, float]]]]:
    """
    Extract start and goal poses from scenario.config file (YAML format).
    
    Args:
        scenario_config_path: Path to scenario.config file
        
    Returns:
        Tuple of (start_pose, goal_poses) where poses are (x, y) tuples, or None if invalid
    """
    try:
        import yaml
        
        with open(scenario_config_path, 'r') as f:
            config = yaml.safe_load(f)
        
        if 'test_scenario' not in config:
            return None
        
        scenario = config['test_scenario']
        
        # Extract start pose
        if 'start_pose' not in scenario:
            return None
        
        start_data = scenario['start_pose']['position']
        start_pose = (float(start_data['x']), float(start_data['y']))
        
        # Extract goal poses
        goal_poses = []
        if 'goal_poses' in scenario:
            for goal in scenario['goal_poses']:
                goal_data = goal['position']
                goal_poses.append((float(goal_data['x']), float(goal_data['y'])))
        
        return start_pose, goal_poses
    except Exception as e:
        print(f"Error parsing scenario.config at {scenario_config_path}: {e}", file=sys.stderr)
        return None


def safe_pearson_corr(x: np.ndarray, y: np.ndarray) -> float:
    """
    Compute Pearson correlation robustly for small/constant samples.

    Args:
        x: First data array
        y: Second data array

    Returns:
        Correlation coefficient, or 0.0 when undefined
    """
    if len(x) < 2 or len(y) < 2:
        return 0.0
    if np.std(x) == 0 or np.std(y) == 0:
        return 0.0

    corr = np.corrcoef(x, y)[0, 1]
    return 0.0 if np.isnan(corr) else float(corr)


def collect_variant_pose_and_distance_stats(variant_path: str) -> Optional[Dict]:
    """
    Collect scenario pose data and run distance metrics for a single variant.

    Args:
        variant_path: Path to a single variant directory containing scenario.config and run folders

    Returns:
        Dictionary with pose and distance statistics, or None if invalid
    """
    variant_dir = Path(variant_path)
    if not variant_dir.is_dir():
        return None

    scenario_config = variant_dir / 'scenario.config'
    if not scenario_config.exists():
        return None

    poses_data = extract_poses_from_scenario_config(str(scenario_config))
    if not poses_data:
        return None

    start_pose, goal_poses = poses_data

    distances = []
    for run_dir in sorted(variant_dir.iterdir()):
        if not run_dir.is_dir() or run_dir.name.startswith('_'):
            continue

        poses_csv = run_dir / 'poses.csv'
        if poses_csv.exists():
            metrics = extract_pose_metrics(str(poses_csv))
            if metrics:
                _, dist_val = metrics
                distances.append(dist_val)

    if len(distances) == 0:
        return None

    all_pose_points = [start_pose] + list(goal_poses)
    pose_array = np.array(all_pose_points, dtype=float)
    goal_array = np.array(goal_poses, dtype=float) if len(goal_poses) > 0 else np.empty((0, 2), dtype=float)

    if len(goal_poses) > 0:
        goal_centroid = (float(np.mean(goal_array[:, 0])), float(np.mean(goal_array[:, 1])))
    else:
        goal_centroid = start_pose

    pose_centroid = (float(np.mean(pose_array[:, 0])), float(np.mean(pose_array[:, 1])))

    return {
        'variant_name': variant_dir.name,
        'variant_path': str(variant_dir),
        'start_pose': start_pose,
        'goal_poses': goal_poses,
        'goal_centroid': goal_centroid,
        'all_pose_points': all_pose_points,
        'pose_centroid': pose_centroid,
        'distances': np.array(distances, dtype=float),
        'num_runs': len(distances),
        'distance_mean': float(np.mean(distances)),
        'distance_var': float(np.var(distances)),
        'distance_std': float(np.std(distances)),
    }


def analyze_pose_distribution_vs_distance_means(variant_paths: List[str]) -> Optional[Dict]:
    """
    Compare variance of pose-position distribution vs variance of per-variant distance means.

    Method:
    1) Build a single distribution using all start/goal positions from each variant's scenario.config.
    2) Build a distribution of mean traveled distances (one mean per variant).
    3) Compare variances and measure correlation between variant pose centroid and variant distance mean.

    Args:
        variant_paths: Paths to variants to include

    Returns:
        Analysis dictionary, or None if insufficient data
    """
    variant_stats = []
    for variant_path in variant_paths:
        stats_data = collect_variant_pose_and_distance_stats(variant_path)
        if stats_data:
            variant_stats.append(stats_data)
        else:
            print(f"Warning: Skipping invalid or incomplete variant: {variant_path}", file=sys.stderr)

    if len(variant_stats) < 2:
        return None

    all_pose_points = []
    for stats_data in variant_stats:
        all_pose_points.extend(stats_data['all_pose_points'])

    pose_array = np.array(all_pose_points, dtype=float)
    pose_norms = np.linalg.norm(pose_array, axis=1)
    distance_means = np.array([v['distance_mean'] for v in variant_stats], dtype=float)

    variant_pose_centroids = np.array([v['pose_centroid'] for v in variant_stats], dtype=float)
    variant_pose_centroid_norms = np.linalg.norm(variant_pose_centroids, axis=1)

    pose_x_var = float(np.var(pose_array[:, 0]))
    pose_y_var = float(np.var(pose_array[:, 1]))
    pose_spatial_var = float(np.var(pose_norms))
    distance_mean_var = float(np.var(distance_means))

    return {
        'num_variants': len(variant_stats),
        'num_pose_samples': len(all_pose_points),
        'variant_stats': variant_stats,
        'pose_points': pose_array,
        'pose_norms': pose_norms,
        'distance_means': distance_means,
        'pose_x_var': pose_x_var,
        'pose_y_var': pose_y_var,
        'pose_spatial_var': pose_spatial_var,
        'distance_mean_var': distance_mean_var,
        'variance_ratio_distance_to_pose_spatial': (
            float(distance_mean_var / pose_spatial_var) if pose_spatial_var > 0 else np.inf
        ),
        'centroid_x_to_distance_mean_corr': safe_pearson_corr(variant_pose_centroids[:, 0], distance_means),
        'centroid_y_to_distance_mean_corr': safe_pearson_corr(variant_pose_centroids[:, 1], distance_means),
        'centroid_spatial_to_distance_mean_corr': safe_pearson_corr(variant_pose_centroid_norms, distance_means),
    }


def compute_pose_difference_between_variants(source_stats: Dict, target_stats: Dict) -> Dict:
    """
    Compute geometric pose differences between two variants using scenario.config poses.

    Args:
        source_stats: Source variant stats from collect_variant_pose_and_distance_stats
        target_stats: Target variant stats from collect_variant_pose_and_distance_stats

    Returns:
        Dictionary with start/goal/combined pose differences
    """
    source_start = np.array(source_stats['start_pose'], dtype=float)
    target_start = np.array(target_stats['start_pose'], dtype=float)
    start_diff = float(np.linalg.norm(source_start - target_start))

    source_goal_centroid = np.array(source_stats['goal_centroid'], dtype=float)
    target_goal_centroid = np.array(target_stats['goal_centroid'], dtype=float)
    goal_centroid_diff = float(np.linalg.norm(source_goal_centroid - target_goal_centroid))

    source_goals = source_stats['goal_poses']
    target_goals = target_stats['goal_poses']
    pairwise_goal_diff = 0.0
    if len(source_goals) > 0 and len(target_goals) > 0:
        pair_count = min(len(source_goals), len(target_goals))
        goal_diffs = []
        for idx in range(pair_count):
            src_goal = np.array(source_goals[idx], dtype=float)
            tgt_goal = np.array(target_goals[idx], dtype=float)
            goal_diffs.append(float(np.linalg.norm(src_goal - tgt_goal)))
        if len(goal_diffs) > 0:
            pairwise_goal_diff = float(np.mean(goal_diffs))

    available_components = [start_diff, goal_centroid_diff]
    if pairwise_goal_diff > 0:
        available_components.append(pairwise_goal_diff)

    combined_pose_diff = float(np.mean(available_components))

    return {
        'start_diff': start_diff,
        'goal_centroid_diff': goal_centroid_diff,
        'pairwise_goal_diff': pairwise_goal_diff,
        'combined_pose_diff': combined_pose_diff,
    }


def analyze_source_pose_vs_distance_differences(source_variant_path: str, other_variant_paths: List[str]) -> Optional[Dict]:
    """
    Compare source-vs-others pose differences against source-vs-others mean-distance differences.

    Args:
        source_variant_path: Path to source variant
        other_variant_paths: Paths to comparison variants

    Returns:
        Analysis dictionary, or None if insufficient data
    """
    source_stats = collect_variant_pose_and_distance_stats(source_variant_path)
    if not source_stats:
        return None

    comparisons = []
    for other_path in other_variant_paths:
        other_stats = collect_variant_pose_and_distance_stats(other_path)
        if not other_stats:
            print(f"Warning: Skipping invalid comparison variant: {other_path}", file=sys.stderr)
            continue

        pose_diff = compute_pose_difference_between_variants(source_stats, other_stats)
        distance_mean_diff = float(abs(other_stats['distance_mean'] - source_stats['distance_mean']))

        comparisons.append({
            'other_variant_name': other_stats['variant_name'],
            'other_variant_path': other_stats['variant_path'],
            'other_distance_mean': other_stats['distance_mean'],
            'other_num_runs': other_stats['num_runs'],
            'distance_mean_diff': distance_mean_diff,
            **pose_diff,
        })

    if len(comparisons) < 2:
        return None

    combined_pose_diffs = np.array([c['combined_pose_diff'] for c in comparisons], dtype=float)
    start_diffs = np.array([c['start_diff'] for c in comparisons], dtype=float)
    goal_centroid_diffs = np.array([c['goal_centroid_diff'] for c in comparisons], dtype=float)
    distance_mean_diffs = np.array([c['distance_mean_diff'] for c in comparisons], dtype=float)

    return {
        'source_variant_name': source_stats['variant_name'],
        'source_variant_path': source_stats['variant_path'],
        'source_distance_mean': source_stats['distance_mean'],
        'source_num_runs': source_stats['num_runs'],
        'num_comparisons': len(comparisons),
        'comparisons': comparisons,
        'combined_pose_diffs': combined_pose_diffs,
        'start_diffs': start_diffs,
        'goal_centroid_diffs': goal_centroid_diffs,
        'distance_mean_diffs': distance_mean_diffs,
        'combined_pose_diff_var': float(np.var(combined_pose_diffs)),
        'distance_mean_diff_var': float(np.var(distance_mean_diffs)),
        'combined_pose_to_distance_diff_corr': safe_pearson_corr(combined_pose_diffs, distance_mean_diffs),
        'start_to_distance_diff_corr': safe_pearson_corr(start_diffs, distance_mean_diffs),
        'goal_centroid_to_distance_diff_corr': safe_pearson_corr(goal_centroid_diffs, distance_mean_diffs),
    }


def print_pose_distribution_vs_distance_means_analysis(analysis: Dict) -> None:
    """Print results for pose-position distribution vs distance-mean distribution analysis."""
    print("\n" + "=" * 90)
    print("POSE-POSITION DISTRIBUTION vs DISTANCE-MEAN DISTRIBUTION")
    print("=" * 90)

    print(f"Variants analyzed:       {analysis['num_variants']}")
    print(f"Total pose samples:      {analysis['num_pose_samples']}")
    print(f"Distance means samples:  {len(analysis['distance_means'])}")

    print("\nVariance summary:")
    print(f"  Pose X variance:                 {analysis['pose_x_var']:.6f}")
    print(f"  Pose Y variance:                 {analysis['pose_y_var']:.6f}")
    print(f"  Pose spatial variance:           {analysis['pose_spatial_var']:.6f}")
    print(f"  Distance-mean variance:          {analysis['distance_mean_var']:.6f}")
    print(f"  Distance/Pose-spatial variance:  {analysis['variance_ratio_distance_to_pose_spatial']:.6f}")

    print("\nCorrelation (variant pose centroid ↔ variant distance mean):")
    print(f"  Centroid X ↔ Distance mean:      {analysis['centroid_x_to_distance_mean_corr']:.6f}")
    print(f"  Centroid Y ↔ Distance mean:      {analysis['centroid_y_to_distance_mean_corr']:.6f}")
    print(f"  Centroid spatial ↔ Distance mean:{analysis['centroid_spatial_to_distance_mean_corr']:.6f}")

    print("\nPer-variant distance means:")
    for variant in analysis['variant_stats']:
        print(f"  {variant['variant_name']}: mean={variant['distance_mean']:.4f} m, runs={variant['num_runs']}")


def print_source_pose_vs_distance_differences_analysis(analysis: Dict) -> None:
    """Print results for source-vs-others pose-difference vs distance-mean-difference analysis."""
    print("\n" + "=" * 90)
    print("SOURCE POSE-DIFFERENCE vs MEAN-DISTANCE-DIFFERENCE")
    print("=" * 90)

    print(f"Source variant:        {analysis['source_variant_name']}")
    print(f"Source mean distance:  {analysis['source_distance_mean']:.4f} m")
    print(f"Comparisons:           {analysis['num_comparisons']}")

    print("\nDistribution variance summary:")
    print(f"  Combined pose-diff variance:      {analysis['combined_pose_diff_var']:.6f}")
    print(f"  Distance-mean-diff variance:      {analysis['distance_mean_diff_var']:.6f}")

    print("\nCorrelation (larger pose difference ↔ larger mean-distance difference):")
    print(f"  Combined pose diff ↔ distance diff: {analysis['combined_pose_to_distance_diff_corr']:.6f}")
    print(f"  Start diff ↔ distance diff:         {analysis['start_to_distance_diff_corr']:.6f}")
    print(f"  Goal centroid diff ↔ distance diff: {analysis['goal_centroid_to_distance_diff_corr']:.6f}")

    print("\nPer-comparison details:")
    for comp in analysis['comparisons']:
        print(
            f"  {analysis['source_variant_name']} vs {comp['other_variant_name']}: "
            f"pose_diff={comp['combined_pose_diff']:.4f}, "
            f"distance_mean_diff={comp['distance_mean_diff']:.4f}"
        )


def plot_pose_distribution_vs_distance_means(analysis: Dict, output_dir: str) -> str:
    """Create plots for pose-position distribution vs distance-mean distribution analysis."""
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    pose_points = analysis['pose_points']
    distance_means = analysis['distance_means']
    variant_names = [v['variant_name'] for v in analysis['variant_stats']]
    variant_centroids = np.array([v['pose_centroid'] for v in analysis['variant_stats']], dtype=float)

    ax = axes[0, 0]
    ax.scatter(pose_points[:, 0], pose_points[:, 1], alpha=0.6, s=50, color='steelblue')
    ax.set_title('All Start/Goal Pose Positions')
    ax.set_xlabel('X')
    ax.set_ylabel('Y')
    ax.grid(True, alpha=0.3)

    ax = axes[0, 1]
    ax.hist(distance_means, bins=min(10, len(distance_means)), alpha=0.7, color='orange', edgecolor='black')
    ax.set_title('Distribution of Variant Mean Distances')
    ax.set_xlabel('Mean Distance (m)')
    ax.set_ylabel('Count')
    ax.grid(True, alpha=0.3)

    ax = axes[1, 0]
    centroid_norms = np.linalg.norm(variant_centroids, axis=1)
    ax.scatter(centroid_norms, distance_means, s=120, alpha=0.7, color='purple')
    for idx, name in enumerate(variant_names):
        ax.annotate(name, (centroid_norms[idx], distance_means[idx]), fontsize=8, ha='center', va='bottom')
    ax.set_title('Variant Pose-Centroid Norm vs Mean Distance')
    ax.set_xlabel('Pose-Centroid Spatial Norm')
    ax.set_ylabel('Mean Distance (m)')
    ax.grid(True, alpha=0.3)

    ax = axes[1, 1]
    ax.axis('off')
    summary = (
        f"num_variants: {analysis['num_variants']}\n"
        f"num_pose_samples: {analysis['num_pose_samples']}\n\n"
        f"pose_x_var: {analysis['pose_x_var']:.6f}\n"
        f"pose_y_var: {analysis['pose_y_var']:.6f}\n"
        f"pose_spatial_var: {analysis['pose_spatial_var']:.6f}\n"
        f"distance_mean_var: {analysis['distance_mean_var']:.6f}\n\n"
        f"centroid_spatial_to_distance_mean_corr:\n"
        f"{analysis['centroid_spatial_to_distance_mean_corr']:.6f}"
    )
    ax.text(0.05, 0.95, summary, transform=ax.transAxes, va='top', fontsize=10, fontfamily='monospace')

    fig.suptitle('Pose Distribution vs Distance-Mean Distribution', fontsize=14, weight='bold')
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, 'pose_distribution_vs_distance_means.png')
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()
    return output_path


def plot_source_pose_vs_distance_differences(analysis: Dict, output_dir: str) -> str:
    """Create plots for source-vs-others pose-difference vs mean-distance-difference analysis."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    combined_pose_diffs = analysis['combined_pose_diffs']
    distance_mean_diffs = analysis['distance_mean_diffs']
    other_names = [c['other_variant_name'] for c in analysis['comparisons']]

    ax = axes[0]
    ax.scatter(combined_pose_diffs, distance_mean_diffs, s=120, alpha=0.7, color='teal')
    for idx, name in enumerate(other_names):
        ax.annotate(name, (combined_pose_diffs[idx], distance_mean_diffs[idx]), fontsize=8, ha='center', va='bottom')
    if len(combined_pose_diffs) > 1 and np.std(combined_pose_diffs) > 0:
        coeffs = np.polyfit(combined_pose_diffs, distance_mean_diffs, 1)
        line = np.poly1d(coeffs)
        x_vals = np.linspace(float(np.min(combined_pose_diffs)), float(np.max(combined_pose_diffs)), 100)
        ax.plot(x_vals, line(x_vals), 'r--', linewidth=2, alpha=0.8, label='Trend')
        ax.legend()
    ax.set_title('Pose Difference vs Mean Distance Difference')
    ax.set_xlabel('Combined Pose Difference')
    ax.set_ylabel('Mean Distance Difference (m)')
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    ax.axis('off')
    summary = (
        f"source: {analysis['source_variant_name']}\n"
        f"source_mean_distance: {analysis['source_distance_mean']:.4f}\n"
        f"comparisons: {analysis['num_comparisons']}\n\n"
        f"combined_pose_diff_var: {analysis['combined_pose_diff_var']:.6f}\n"
        f"distance_mean_diff_var: {analysis['distance_mean_diff_var']:.6f}\n\n"
        f"combined_pose_to_distance_diff_corr:\n"
        f"{analysis['combined_pose_to_distance_diff_corr']:.6f}"
    )
    ax.text(0.05, 0.95, summary, transform=ax.transAxes, va='top', fontsize=10, fontfamily='monospace')

    fig.suptitle('Source vs Others: Pose-Diff vs Distance-Mean-Diff', fontsize=14, weight='bold')
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"source_pose_vs_distance_diff_{analysis['source_variant_name']}.png")
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()
    return output_path


def analyze_pose_variance_correlation(test_type_base_path: str) -> Optional[Dict]:
    """
    Analyze correlation between scenario config pose variance and navigation distance variance.
    
    For a test type base (e.g., "mt-geometric-gaussian"), finds all variants (1-1, 1-2, etc.),
    extracts poses from each variant's scenario.config, and correlates configuration-level
    pose variance with the distance variance observed across all runs in that variant.
    
    Args:
        test_type_base_path: Base path containing test type variants (parent directory)
        
    Returns:
        Dictionary with pose variance and distance correlation analysis, or None if insufficient data
    """
    parent_dir = Path(test_type_base_path).parent
    test_type_base = Path(test_type_base_path).name.rsplit('-', 2)[0]  # Remove -1-1 or -1-2 suffix
    
    variant_analyses = []
    
    # Find all variants of this test type
    for potential_variant in sorted(parent_dir.glob(f"{test_type_base}-*")):
        if not potential_variant.is_dir():
            continue
        
        scenario_config = potential_variant / 'scenario.config'
        if not scenario_config.exists():
            continue
        
        # Extract poses from scenario config
        poses_data = extract_poses_from_scenario_config(str(scenario_config))
        if not poses_data:
            continue
        
        start_pose, goal_poses = poses_data
        
        # Extract distance variance from all runs in this variant
        distances = []
        for run_dir in sorted(potential_variant.iterdir()):
            if not run_dir.is_dir():
                continue
            
            poses_csv = run_dir / 'poses.csv'
            if poses_csv.exists():
                metrics = extract_pose_metrics(str(poses_csv))
                if metrics:
                    time_val, dist_val = metrics
                    distances.append(dist_val)
        
        if len(distances) > 0:
            distance_var = float(np.var(distances))
            distance_mean = float(np.mean(distances))
            
            # Compute centroid of goal poses
            if goal_poses:
                goal_array = np.array(goal_poses)
                goal_centroid = (float(np.mean(goal_array[:, 0])), float(np.mean(goal_array[:, 1])))
                goal_x_var = float(np.var(goal_array[:, 0]))
                goal_y_var = float(np.var(goal_array[:, 1]))
                goal_spatial_var = float(np.var(np.linalg.norm(goal_array, axis=1)))
            else:
                goal_centroid = (0.0, 0.0)
                goal_x_var = 0.0
                goal_y_var = 0.0
                goal_spatial_var = 0.0
            
            variant_analyses.append({
                'variant_name': potential_variant.name,
                'start_pose': start_pose,
                'goal_poses': goal_poses,
                'goal_centroid': goal_centroid,
                'goal_x_var': goal_x_var,
                'goal_y_var': goal_y_var,
                'goal_spatial_var': goal_spatial_var,
                'num_runs': len(distances),
                'distances': np.array(distances),
                'distance_var': distance_var,
                'distance_mean': distance_mean,
                'distance_std': float(np.std(distances)),
            })
    
    if len(variant_analyses) < 2:
        return None
    
    # Aggregate data across variants
    all_start_x = [v['start_pose'][0] for v in variant_analyses]
    all_start_y = [v['start_pose'][1] for v in variant_analyses]
    all_goal_centroids = [v['goal_centroid'] for v in variant_analyses]
    all_distance_vars = [v['distance_var'] for v in variant_analyses]
    all_distance_means = [v['distance_mean'] for v in variant_analyses]
    
    # Calculate variance of poses across variants
    start_x_var_across_variants = float(np.var(all_start_x))
    start_y_var_across_variants = float(np.var(all_start_y))
    start_spatial_var = float(np.var(np.linalg.norm(np.array([all_start_x, all_start_y]).T, axis=1)))
    
    goal_centroid_array = np.array(all_goal_centroids)
    goal_x_var_across_variants = float(np.var(goal_centroid_array[:, 0]))
    goal_y_var_across_variants = float(np.var(goal_centroid_array[:, 1]))
    goal_spatial_var_across_variants = float(np.var(np.linalg.norm(goal_centroid_array, axis=1)))
    
    # Correlate pose variance with distance variance
    combined_pose_var = start_spatial_var + goal_spatial_var_across_variants
    
    correlation_pose_distance = float(np.corrcoef(
        np.array(all_distance_vars),
        combined_pose_var * np.ones(len(all_distance_vars))
    )[0, 1]) if len(all_distance_vars) > 1 else 0.0
    
    # Correlate start position with distance variance
    start_x_distance_corr = float(np.corrcoef(all_start_x, all_distance_vars)[0, 1]) if len(all_start_x) > 1 else 0.0
    start_y_distance_corr = float(np.corrcoef(all_start_y, all_distance_vars)[0, 1]) if len(all_start_y) > 1 else 0.0
    
    # Correlate goal position with distance variance
    goal_x_distance_corr = float(np.corrcoef(goal_centroid_array[:, 0], all_distance_vars)[0, 1]) if len(all_distance_vars) > 1 else 0.0
    goal_y_distance_corr = float(np.corrcoef(goal_centroid_array[:, 1], all_distance_vars)[0, 1]) if len(all_distance_vars) > 1 else 0.0
    
    # Correlate spatial distances with distance variance
    start_spatial_distances = np.linalg.norm(np.array([all_start_x, all_start_y]).T, axis=1)
    goal_spatial_distances = np.linalg.norm(goal_centroid_array, axis=1)
    start_spatial_distance_corr = float(np.corrcoef(start_spatial_distances, all_distance_vars)[0, 1]) if len(all_distance_vars) > 1 else 0.0
    goal_spatial_distance_corr = float(np.corrcoef(goal_spatial_distances, all_distance_vars)[0, 1]) if len(all_distance_vars) > 1 else 0.0
    
    return {
        'test_type_base': test_type_base,
        'num_variants': len(variant_analyses),
        'variant_analyses': variant_analyses,
        'start_x_var': start_x_var_across_variants,
        'start_y_var': start_y_var_across_variants,
        'start_spatial_var': start_spatial_var,
        'goal_x_var': goal_x_var_across_variants,
        'goal_y_var': goal_y_var_across_variants,
        'goal_spatial_var': goal_spatial_var_across_variants,
        'combined_pose_var': combined_pose_var,
        'distance_variances': np.array(all_distance_vars),
        'distance_means': np.array(all_distance_means),
        'start_x_distance_corr': start_x_distance_corr,
        'start_y_distance_corr': start_y_distance_corr,
        'goal_x_distance_corr': goal_x_distance_corr,
        'goal_y_distance_corr': goal_y_distance_corr,
        'start_spatial_distance_corr': start_spatial_distance_corr,
        'goal_spatial_distance_corr': goal_spatial_distance_corr,
    }


def is_successful_test_run(test_xml_path: Path) -> bool:
    """
    Check whether a test run is successful based on failures=0 in test.xml.

    Args:
        test_xml_path: Path to the run's test.xml file

    Returns:
        True if failures attribute equals 0, False otherwise
    """
    try:
        root = ET.parse(test_xml_path).getroot()
        failures = root.attrib.get('failures')

        if failures is None:
            return False

        return float(failures) == 0.0
    except Exception as e:
        print(f"  Could not parse {test_xml_path}: {e} (skipped)", file=sys.stderr)
        return False


def process_test_type(test_type_path: str, successful_only: bool = False, 
                     extract_localization: bool = False) -> Tuple[List[float], List[float], List[float], List[float]]:
    """
    Process all runs in a test type folder and extract metrics.
    
    Args:
        test_type_path: Path to the test type folder containing run subfolders
        successful_only: If True, include only runs with failures=0 in test.xml
        extract_localization: If True, also extract localization error metrics
        
    Returns:
        Tuple of (times_list, distances_list, loc_means_list, loc_vars_list)
        If extract_localization is False, loc_means_list and loc_vars_list will be empty
    """
    times = []
    distances = []
    loc_means = []
    loc_vars = []
    
    test_type_dir = Path(test_type_path)
    
    # Iterate through run folders (0, 1, 2, ...)
    for run_dir in sorted(test_type_dir.iterdir()):
        if not run_dir.is_dir() or run_dir.name.startswith('_'):
            continue

        if successful_only:
            test_xml_path = run_dir / 'test.xml'
            if not test_xml_path.exists():
                print(f"  Run {run_dir.name}: No test.xml found (skipped)")
                continue

            if not is_successful_test_run(test_xml_path):
                print(f"  Run {run_dir.name}: test.xml failures != 0 (skipped)")
                continue
        
        poses_csv = run_dir / 'poses.csv'
        
        if poses_csv.exists():
            result = extract_pose_metrics(str(poses_csv))
            if result is not None:
                time_taken, distance_traveled = result
                times.append(time_taken)
                distances.append(distance_traveled)
                
                # Extract localization error if requested
                if extract_localization:
                    loc_error_csv = run_dir / 'localization_error.csv'
                    if loc_error_csv.exists():
                        loc_result = extract_localization_error_metrics(str(loc_error_csv))
                        if loc_result is not None:
                            mean_cov, var_cov = loc_result
                            loc_means.append(mean_cov)
                            loc_vars.append(var_cov)
                            print(f"  Run {run_dir.name}: time={time_taken:.2f}s, distance={distance_traveled:.2f}m, "
                                  f"loc_mean={mean_cov:.4f}m, loc_var={var_cov:.6f}m²")
                        else:
                            print(f"  Run {run_dir.name}: time={time_taken:.2f}s, distance={distance_traveled:.2f}m, "
                                  f"localization_error.csv failed to process", file=sys.stderr)
                    else:
                        print(f"  Run {run_dir.name}: time={time_taken:.2f}s, distance={distance_traveled:.2f}m, "
                              f"no localization_error.csv (skipped)")
                else:
                    print(f"  Run {run_dir.name}: time={time_taken:.2f}s, distance={distance_traveled:.2f}m")
            else:
                print(f"  Run {run_dir.name}: Failed to process", file=sys.stderr)
        else:
            print(f"  Run {run_dir.name}: No poses.csv found (skipped)")
    
    return times, distances, loc_means, loc_vars


def save_metrics_to_csv(test_type_name: str, times: List[float], 
                       distances: List[float], output_dir: str,
                       loc_means: Optional[List[float]] = None,
                       loc_vars: Optional[List[float]] = None) -> Tuple[str, ...]:
    """
    Save time, distance, and optionally localization error metrics to separate CSV files.
    
    Args:
        test_type_name: Name of the test type
        times: List of time values
        distances: List of distance values
        output_dir: Directory to save CSV files
        loc_means: Optional list of mean covariance values
        loc_vars: Optional list of covariance variance values
        
    Returns:
        Tuple of CSV paths (time, distance, [loc_mean, loc_var] if provided)
    """
    os.makedirs(output_dir, exist_ok=True)
    
    # Save times
    time_csv_path = os.path.join(output_dir, f"{test_type_name}_times.csv")
    with open(time_csv_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['run_index', 'time_seconds'])
        for i, time_val in enumerate(times):
            writer.writerow([i, time_val])
    
    # Save distances
    distance_csv_path = os.path.join(output_dir, f"{test_type_name}_distances.csv")
    with open(distance_csv_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['run_index', 'distance_meters'])
        for i, dist_val in enumerate(distances):
            writer.writerow([i, dist_val])
    
    result_paths = [time_csv_path, distance_csv_path]
    
    # Save localization error means if provided
    if loc_means is not None and len(loc_means) > 0:
        loc_mean_csv_path = os.path.join(output_dir, f"{test_type_name}_loc_error_means.csv")
        with open(loc_mean_csv_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['run_index', 'mean_covariance_meters'])
            for i, mean_cov in enumerate(loc_means):
                writer.writerow([i, mean_cov])
        result_paths.append(loc_mean_csv_path)
    
    # Save localization error variances if provided
    if loc_vars is not None and len(loc_vars) > 0:
        loc_var_csv_path = os.path.join(output_dir, f"{test_type_name}_loc_error_vars.csv")
        with open(loc_var_csv_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['run_index', 'variance_covariance_meters_squared'])
            for i, var_cov in enumerate(loc_vars):
                writer.writerow([i, var_cov])
        result_paths.append(loc_var_csv_path)
    
    return tuple(result_paths)


def fit_distributions(data: np.ndarray) -> Dict[str, Tuple]:
    """
    Fit multiple distributions to data and return parameters.
    
    Args:
        data: Array of numerical data
        
    Returns:
        Dictionary mapping distribution names to (params, fitted_data)
    """
    distributions = {}
    
    try:
        # Normal distribution
        params = norm.fit(data)
        distributions['normal'] = params
    except:
        pass
    
    try:
        # Exponential distribution
        params = expon.fit(data)
        distributions['exponential'] = params
    except:
        pass
    
    try:
        # Lognormal distribution
        params = lognorm.fit(data)
        distributions['lognormal'] = params
    except:
        pass
    
    try:
        # Gamma distribution
        params = gamma.fit(data)
        distributions['gamma'] = params
    except:
        pass
    
    try:
        # Weibull distribution
        params = weibull_min.fit(data)
        distributions['weibull'] = params
    except:
        pass
    
    try:
        # Poisson distribution
        # Estimate lambda parameter using method of moments
        mu = np.mean(data)
        distributions['poisson'] = (mu,)
    except:
        pass
    
    return distributions


def compute_goodness_of_fit(data: np.ndarray, dist_name: str, 
                           params: Tuple) -> Dict[str, float]:
    """
    Compute goodness of fit statistics for a distribution.
    
    Args:
        data: Array of numerical data
        dist_name: Name of the distribution
        params: Parameters of the distribution
        
    Returns:
        Dictionary of fit statistics
    """
    stats_dict = {}
    
    # Map distribution names to scipy distributions
    dist_map = {
        'normal': norm,
        'exponential': expon,
        'lognormal': lognorm,
        'gamma': gamma,
        'weibull': weibull_min,
        'poisson': poisson
    }
    
    if dist_name not in dist_map:
        return stats_dict
    
    dist = dist_map[dist_name]
    
    try:
        # Kolmogorov-Smirnov test
        ks_stat, ks_pval = kstest(data, lambda x: dist.cdf(x, *params))
        stats_dict['ks_statistic'] = ks_stat
        stats_dict['ks_pvalue'] = ks_pval
    except:
        pass
    
    try:
        # Anderson-Darling test (only for normal, exponential)
        if dist_name in ['normal', 'exponential']:
            ad_result = anderson(data, dist_name)
            stats_dict['anderson_statistic'] = ad_result.statistic
    except:
        pass
    
    return stats_dict


def analyze_distribution_fit(data: np.ndarray) -> Tuple[str, Dict]:
    """
    Analyze which distribution fits the data best.
    
    Args:
        data: Array of numerical data
        
    Returns:
        Tuple of (best_dist_name, analysis_dict)
    """
    analysis = {
        'sample_size': len(data),
        'mean': float(np.mean(data)),
        'std': float(np.std(data)),
        'median': float(np.median(data)),
        'min': float(np.min(data)),
        'max': float(np.max(data)),
        'skewness': float(stats.skew(data)), # < 0 is long left tail, > 0 is long right tail
        'kurtosis': float(stats.kurtosis(data)), # < 0 is light-tailed, > 0 is heavy-tailed
    }
    
    # Test for normality
    try:
        _, norm_pval = shapiro(data)
        analysis['shapiro_pvalue'] = norm_pval
        analysis['shapiro_normal'] = norm_pval > 0.05
    except:
        analysis['shapiro_normal'] = False
    
    # Jarque-Bera test for normality
    try:
        jb_stat, jb_pval = jarque_bera(data)
        analysis['jarque_bera_statistic'] = float(jb_stat)
        analysis['jarque_bera_pvalue'] = jb_pval
        analysis['jarque_bera_normal'] = jb_pval > 0.05
    except:
        pass
    
    # Fit distributions
    fitted_dists = fit_distributions(data)
    
    best_dist = 'normal'
    best_ks_pval = -1
    
    distribution_fits = {}
    
    for dist_name, params in fitted_dists.items():
        fit_stats = compute_goodness_of_fit(data, dist_name, params)
        distribution_fits[dist_name] = {
            'parameters': params,
            'fit_stats': fit_stats
        }
        
        # Track best fit based on KS test p-value
        if 'ks_pvalue' in fit_stats:
            if fit_stats['ks_pvalue'] > best_ks_pval:
                best_ks_pval = fit_stats['ks_pvalue']
                best_dist = dist_name
    
    analysis['distribution_fits'] = distribution_fits
    analysis['best_fit_distribution'] = best_dist
    
    return best_dist, analysis


def compare_two_distributions(name1: str, data1: np.ndarray, 
                             name2: str, data2: np.ndarray) -> Dict:
    """
    Compare two distributions using multiple nonparametric tests.
    
    Args:
        name1: Name of first test type
        data1: Data from first test type
        name2: Name of second test type
        data2: Data from second test type
        
    Returns:
        Dictionary with comparison results
    """
    results = {
        'test_1': name1,
        'test_2': name2,
        'sample_size_1': len(data1),
        'sample_size_2': len(data2),
        'mean_1': float(np.mean(data1)),
        'mean_2': float(np.mean(data2)),
        'std_1': float(np.std(data1)),
        'std_2': float(np.std(data2)),
        'median_1': float(np.median(data1)),
        'median_2': float(np.median(data2)),
    }
    
    # Mann-Whitney U test: Tests if two distributions have the same location (median).
    # Non-parametric alternative to t-test. Robust against non-normal data.
    # Most powerful for detecting shifts in central tendency.
    try:
        u_stat, u_pval = stats.mannwhitneyu(data1, data2, alternative='two-sided')
        results['mann_whitney_u'] = float(u_stat)
        results['mann_whitney_pvalue'] = float(u_pval)
        results['mann_whitney_significant'] = u_pval < 0.05
    except:
        pass
    
    # Kolmogorov-Smirnov test: Tests if two distributions are fundamentally different
    # across their entire cumulative distribution function (shape and location).
    # More sensitive to differences in the tails of distributions.
    try:
        ks_stat, ks_pval = stats.ks_2samp(data1, data2)
        results['ks_statistic'] = float(ks_stat)
        results['ks_pvalue'] = float(ks_pval)
        results['ks_significant'] = ks_pval < 0.05
    except:
        pass
    
    # Levene's test: Tests if two distributions have equal variances.
    # More robust than Bartlett's test. Recommended for non-normal data.
    # Uses absolute deviations from the mean.
    try:
        levene_stat, levene_pval = stats.levene(data1, data2)
        results['levene_statistic'] = float(levene_stat)
        results['levene_pvalue'] = float(levene_pval)
        results['levene_significant'] = levene_pval < 0.05
    except:
        pass
    
    # Fligner-Killeen test: Non-parametric test for equal variances.
    # Less sensitive to outliers than Levene's. Better for severely non-normal distributions.
    # Uses ranks instead of actual values.
    try:
        fk_stat, fk_pval = stats.fligner(data1, data2)
        results['fligner_statistic'] = float(fk_stat)
        results['fligner_pvalue'] = float(fk_pval)
        results['fligner_significant'] = fk_pval < 0.05
    except:
        pass
    

    # Brunner-Munzel test: Non-parametric test for stochastic dominance.
    # More robust than Mann-Whitney when variances are unequal.
    # Tests if one distribution tends to have larger values than the other.
    try:
        bm_stat, bm_pval = stats.brunnermunzel(data1, data2)
        # Check for NaN p-value (occurs when distributions are completely separated)
        if np.isnan(bm_pval):
            # When completely separated, distributions are definitively different
            bm_pval = 0.0 if (np.mean(data1) != np.mean(data2)) else 1.0
            bm_stat = np.inf if not np.isfinite(bm_stat) else bm_stat
        results['brunner_munzel_statistic'] = float(bm_stat)
        results['brunner_munzel_pvalue'] = float(bm_pval)
        results['brunner_munzel_significant'] = bm_pval < 0.05
    except:
        pass
    
    return results


def plot_distribution(name: str, data: np.ndarray, analysis: Dict, 
                     metric_name: str, output_dir: str) -> str:
    """
    Plot histogram with the best-fit distribution curve.
    
    Args:
        name: Name of the test type
        data: Array of numerical data
        analysis: Analysis dictionary containing fit information
        metric_name: Name of the metric (Time or Distance)
        output_dir: Directory to save the plot
        
    Returns:
        Path to the saved figure
    """
    fig, ax = plt.subplots(figsize=(10, 6))
    
    # Plot histogram
    ax.hist(data, bins=20, density=True, alpha=0.7, color='steelblue', edgecolor='black')
    
    # Plot only the best-fit distribution
    dist_map = {
        'normal': norm,
        'exponential': expon,
        'lognormal': lognorm,
        'gamma': gamma,
        'weibull': weibull_min,
        'poisson': poisson,
    }
    
    best_dist_name = analysis['best_fit_distribution']
    if best_dist_name in dist_map:
        fit_info = analysis['distribution_fits'].get(best_dist_name, {})
        params = fit_info.get('parameters')
        
        if params is not None:
            dist = dist_map[best_dist_name]
            try:
                # Generate appropriate x range for this distribution
                x_min = max(data.min() * 0.95, 0) if best_dist_name in ['exponential', 'gamma', 'weibull', 'lognormal', 'poisson'] else data.min() * 0.95
                x_max = data.max() * 1.05
                
                # Handle discrete (poisson) vs continuous distributions
                if best_dist_name == 'poisson':
                    # For poisson, use integer x values and PMF
                    x = np.arange(int(x_min), int(x_max) + 1)
                    y = dist.pmf(x, *params)
                    ax.plot(x, y, 'ro-', linewidth=2, markersize=4, label=f'{best_dist_name} fit')
                else:
                    # For continuous distributions, use PDF
                    x = np.linspace(x_min, x_max, 200)
                    y = dist.pdf(x, *params)
                    ax.plot(x, y, color='red', linewidth=2.5, label=f'{best_dist_name} fit')
            except Exception as e:
                print(f"Warning: Could not plot {best_dist_name} for {name}: {e}", file=sys.stderr)
    
    ax.set_xlabel(metric_name)
    ax.set_ylabel('Density')
    ax.set_title(f'{metric_name} Distribution: {name}\n(Best fit: {analysis["best_fit_distribution"]})')
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)
    
    # Save figure
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"distribution_{name}_{metric_name.lower().replace(' ', '_')}.png")
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()
    
    return output_path


def plot_comparison(name1: str, data1: np.ndarray, analysis1: Dict, 
                   name2: str, data2: np.ndarray, analysis2: Dict,
                   metric_name: str, output_dir: str) -> str:
    """
    Create comparison plots overlaying best-fit distributions of two test types.
    
    Args:
        name1: Name of first test type
        data1: Data from first test type
        analysis1: Analysis dictionary for first test type
        name2: Name of second test type
        data2: Data from second test type
        analysis2: Analysis dictionary for second test type
        metric_name: Name of the metric (Time or Distance)
        output_dir: Directory to save the plot
        
    Returns:
        Path to the saved figure
    """
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    
    # Plot 1: Overlaid histograms with fitted distributions
    ax = axes[0]
    ax.hist(data1, bins=15, alpha=0.5, color='steelblue', edgecolor='black', density=True)
    ax.hist(data2, bins=15, alpha=0.5, color='orange', edgecolor='black', density=True)
    
    # Plot best-fit distributions for both test types
    dist_map = {
        'normal': norm,
        'exponential': expon,
        'lognormal': lognorm,
        'gamma': gamma,
        'weibull': weibull_min,
        'poisson': poisson,
    }
    
    # Plot distribution 1
    dist1_name = analysis1['best_fit_distribution']
    if dist1_name in dist_map:
        fit_info1 = analysis1['distribution_fits'].get(dist1_name, {})
        params1 = fit_info1.get('parameters')
        if params1 is not None:
            try:
                # Generate appropriate x range for this distribution
                x1_min = max(data1.min() * 0.95, 0) if dist1_name in ['exponential', 'gamma', 'weibull', 'lognormal', 'poisson'] else data1.min() * 0.95
                x1_max = data1.max() * 1.05
                
                # Handle discrete (poisson) vs continuous distributions
                if dist1_name == 'poisson':
                    x1 = np.arange(int(x1_min), int(x1_max) + 1)
                    y1 = dist_map[dist1_name].pmf(x1, *params1)
                    ax.plot(x1, y1, 'o-', color='darkblue', linewidth=2, markersize=4, label=f'{name1} ({dist1_name})')
                else:
                    x1 = np.linspace(x1_min, x1_max, 200)
                    y1 = dist_map[dist1_name].pdf(x1, *params1)
                    ax.plot(x1, y1, color='darkblue', linewidth=2.5, label=f'{name1} ({dist1_name})')
            except Exception as e:
                print(f"Warning: Could not plot {dist1_name} for {name1}: {e}", file=sys.stderr)
    
    # Plot distribution 2
    dist2_name = analysis2['best_fit_distribution']
    if dist2_name in dist_map:
        fit_info2 = analysis2['distribution_fits'].get(dist2_name, {})
        params2 = fit_info2.get('parameters')
        if params2 is not None:
            try:
                # Generate appropriate x range for this distribution
                x2_min = max(data2.min() * 0.95, 0) if dist2_name in ['exponential', 'gamma', 'weibull', 'lognormal', 'poisson'] else data2.min() * 0.95
                x2_max = data2.max() * 1.05
                
                # Handle discrete (poisson) vs continuous distributions
                if dist2_name == 'poisson':
                    x2 = np.arange(int(x2_min), int(x2_max) + 1)
                    y2 = dist_map[dist2_name].pmf(x2, *params2)
                    ax.plot(x2, y2, 'o-', color='darkorange', linewidth=2, markersize=4, label=f'{name2} ({dist2_name})')
                else:
                    x2 = np.linspace(x2_min, x2_max, 200)
                    y2 = dist_map[dist2_name].pdf(x2, *params2)
                    ax.plot(x2, y2, color='darkorange', linewidth=2.5, label=f'{name2} ({dist2_name})')
            except Exception as e:
                print(f"Warning: Could not plot {dist2_name} for {name2}: {e}", file=sys.stderr)
    
    ax.set_xlabel(metric_name)
    ax.set_ylabel('Density')
    ax.set_title('Overlaid Distributions with Fits')
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)
    
    # Plot 2: Box plots for comparison
    ax = axes[1]
    box_data = [data1, data2]
    bp = ax.boxplot(box_data, labels=[name1, name2], patch_artist=True)
    
    # Color the boxes
    colors = ['steelblue', 'orange']
    for patch, color in zip(bp['boxes'], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)
    
    ax.set_ylabel(metric_name)
    ax.set_title('Summary Statistics')
    ax.grid(True, alpha=0.3, axis='y')
    
    # Save figure
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"comparison_{name1}_vs_{name2}_{metric_name.lower().replace(' ', '_')}.png")
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()
    
    return output_path


def plot_pose_variance_correlation(test_type_base_name: str, analysis: Dict, output_dir: str) -> str:
    """
    Plot pose variance analysis across test type variants and correlation with distance.
    
    Args:
        test_type_base_name: Base name of the test type (e.g., mt-geometric-gaussian)
        analysis: Analysis dictionary from analyze_pose_variance_correlation
        output_dir: Directory to save the plot
        
    Returns:
        Path to the saved figure
    """
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    
    variant_analyses = analysis['variant_analyses']
    variant_names = [v['variant_name'] for v in variant_analyses]
    start_poses = [v['start_pose'] for v in variant_analyses]
    goal_centroids = [v['goal_centroid'] for v in variant_analyses]
    distance_vars = [v['distance_var'] for v in variant_analyses]
    
    # Plot 1: Start poses across variants
    ax = axes[0, 0]
    start_poses_array = np.array(start_poses)
    ax.scatter(start_poses_array[:, 0], start_poses_array[:, 1], s=150, alpha=0.7, color='steelblue')
    for i, name in enumerate(variant_names):
        ax.annotate(name, (start_poses_array[i, 0], start_poses_array[i, 1]), 
                   fontsize=8, ha='center', va='bottom')
    ax.set_xlabel('Start Position X (m)')
    ax.set_ylabel('Start Position Y (m)')
    ax.set_title('Start Pose Variance Across Variants')
    ax.grid(True, alpha=0.3)
    
    # Plot 2: Goal centroids across variants
    ax = axes[0, 1]
    goal_cents_array = np.array(goal_centroids)
    ax.scatter(goal_cents_array[:, 0], goal_cents_array[:, 1], s=150, alpha=0.7, color='orange')
    for i, name in enumerate(variant_names):
        ax.annotate(name, (goal_cents_array[i, 0], goal_cents_array[i, 1]), 
                   fontsize=8, ha='center', va='bottom')
    ax.set_xlabel('Goal Centroid X (m)')
    ax.set_ylabel('Goal Centroid Y (m)')
    ax.set_title('Goal Pose Centroid Variance Across Variants')
    ax.grid(True, alpha=0.3)
    
    # Plot 3: Pose configuration variance vs distance variance
    ax = axes[1, 0]
    start_x_vals = [v['start_pose'][0] for v in variant_analyses]
    
    ax.scatter(start_x_vals, distance_vars, s=150, alpha=0.7, color='steelblue')
    
    # Add trend line if enough data
    if len(start_x_vals) > 1:
        z = np.polyfit(start_x_vals, distance_vars, 1)
        p = np.poly1d(z)
        x_trend = np.linspace(np.min(start_x_vals), np.max(start_x_vals), 100)
        ax.plot(x_trend, p(x_trend), "r--", alpha=0.8, linewidth=2, label='Trend')
    
    for i, name in enumerate(variant_names):
        ax.annotate(name, (start_x_vals[i], distance_vars[i]), 
                   fontsize=8, ha='center', va='bottom')
    
    ax.set_xlabel('Start Position X (m)')
    ax.set_ylabel('Distance Variance (m²)')
    ax.set_title(f'Configuration Pose Variance ↔ Distance Variance\n'
                f'(Start X corr: {analysis["start_x_distance_corr"]:.3f})')
    ax.legend()
    ax.grid(True, alpha=0.3)
    
    # Plot 4: Summary statistics table
    ax = axes[1, 1]
    ax.axis('off')
    
    summary_text = f"""
Test Type: {analysis['test_type_base']}
Number of Variants: {analysis['num_variants']}

Pose Variance (across variants):
  Start X: {analysis['start_x_var']:.6f} m²
  Start Y: {analysis['start_y_var']:.6f} m²
  Goal X:  {analysis['goal_x_var']:.6f} m²
  Goal Y:  {analysis['goal_y_var']:.6f} m²

Distance Variance (across runs):
  Min: {np.min(analysis['distance_variances']):.1f} m²
  Max: {np.max(analysis['distance_variances']):.1f} m²
  Mean: {np.mean(analysis['distance_variances']):.1f} m²

Correlations (config → run):
  Start X ↔ Distance:  {analysis['start_x_distance_corr']:.4f}
  Start Y ↔ Distance:  {analysis['start_y_distance_corr']:.4f}
"""
    
    ax.text(0.1, 0.9, summary_text, transform=ax.transAxes, 
           fontsize=10, verticalalignment='top', fontfamily='monospace',
           bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.3))
    
    fig.suptitle(f'Pose Variance Correlation: {analysis["test_type_base"]}', 
                fontsize=14, weight='bold')
    
    # Save figure
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"pose_variance_correlation_{analysis['test_type_base']}.png")
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()
    
    return output_path


def print_distribution_analysis(name: str, analysis: Dict, metric_name: str):
    """
    Print distribution analysis results.
    
    Args:
        name: Name of the test type
        analysis: Analysis dictionary
        metric_name: Name of the metric (Time or Distance)
    """
    print(f"\n{'='*70}")
    print(f"{metric_name} Distribution Analysis: {name}")
    print(f"{'='*70}")
    print(f"Sample size: {analysis['sample_size']}")
    print(f"Mean: {analysis['mean']:.4f}")
    print(f"Std Dev: {analysis['std']:.4f}")
    print(f"Median: {analysis['median']:.4f}")
    print(f"Min: {analysis['min']:.4f}")
    print(f"Max: {analysis['max']:.4f}")
    print(f"Skewness: {analysis['skewness']:.4f}")
    print(f"Kurtosis: {analysis['kurtosis']:.4f}")
    
    if analysis.get('shapiro_normal'):
        print(f"Shapiro-Wilk p-value: {analysis['shapiro_pvalue']:.4f} (Normal)")
    else:
        print(f"Shapiro-Wilk p-value: {analysis['shapiro_pvalue']:.4f} (Not Normal)")
    
    if 'jarque_bera_pvalue' in analysis:
        if analysis['jarque_bera_normal']:
            print(f"Jarque-Bera p-value: {analysis['jarque_bera_pvalue']:.4f} (Normal)")
        else:
            print(f"Jarque-Bera p-value: {analysis['jarque_bera_pvalue']:.4f} (Not Normal)")
    
    print(f"\nBest fit distribution: {analysis['best_fit_distribution']}")
    
    print("\nDistribution Fit Statistics:")
    for dist_name, fit_info in analysis['distribution_fits'].items():
        print(f"  {dist_name}:")
        if 'fit_stats' in fit_info and 'ks_pvalue' in fit_info['fit_stats']:
            ks_pval = fit_info['fit_stats']['ks_pvalue']
            print(f"    KS Test p-value: {ks_pval:.4f}")


def print_pose_variance_analysis(test_type_base: str, analysis: Dict) -> None:
    """
    Print pose variance correlation analysis results.
    
    Args:
        test_type_base: Base name of the test type
        analysis: Analysis dictionary from analyze_pose_variance_correlation
    """
    print("\n" + "="*90)
    print(f"POSE VARIANCE CORRELATION ANALYSIS: {test_type_base}")
    print("="*90)
    
    print(f"\nTest Type:          {analysis['test_type_base']}")
    print(f"Total Variants:     {analysis['num_variants']}")
    print(f"Distance Variances: Min={np.min(analysis['distance_variances']):.1f} m², "
          f"Max={np.max(analysis['distance_variances']):.1f} m², "
          f"Mean={np.mean(analysis['distance_variances']):.1f} m²")
    
    # Per-variant breakdown
    print("\n" + "-"*90)
    print("PER-VARIANT ANALYSIS:")
    print("-"*90)
    print(f"{'Variant':<30} {'# Runs':<10} {'Start Pose':<20} {'Goal Centroid':<20} {'Distance Var':<15}")
    print("-"*90)
    
    for variant_analysis in analysis['variant_analyses']:
        variant_name = variant_analysis['variant_name']
        num_runs = variant_analysis['num_runs']
        start_pose = variant_analysis['start_pose']
        goal_centroid = variant_analysis['goal_centroid']
        distance_var = variant_analysis['distance_var']
        
        start_str = f"({start_pose[0]:.3f}, {start_pose[1]:.3f})"
        goal_str = f"({goal_centroid[0]:.3f}, {goal_centroid[1]:.3f})"
        
        print(f"{variant_name:<30} {num_runs:<10} {start_str:<20} {goal_str:<20} {distance_var:>13.1f} m²")
    
    # Configuration-level pose variance (across variants)
    print("\n" + "-"*90)
    print("CONFIGURATION-LEVEL POSE VARIANCE (Across Variants):")
    print("-"*90)
    print(f"  Start Position X:   {analysis['start_x_var']:.6f} m²")
    print(f"  Start Position Y:   {analysis['start_y_var']:.6f} m²")
    print(f"  Start Spatial:      {analysis['start_spatial_var']:.6f} m²")
    print(f"  Goal Position X:    {analysis['goal_x_var']:.6f} m²")
    print(f"  Goal Position Y:    {analysis['goal_y_var']:.6f} m²")
    print(f"  Goal Spatial:       {analysis['goal_spatial_var']:.6f} m²")
    
    # Correlation coefficients
    print("\n" + "-"*90)
    print("CORRELATION: Configuration Pose Variance ↔ Run Distance Variance:")
    print("-"*90)
    print(f"  Start X ↔ Distance Var:     {analysis['start_x_distance_corr']:.6f}")
    print(f"  Start Y ↔ Distance Var:     {analysis['start_y_distance_corr']:.6f}")
    print(f"  Goal X ↔ Distance Var:      {analysis['goal_x_distance_corr']:.6f}")
    print(f"  Goal Y ↔ Distance Var:      {analysis['goal_y_distance_corr']:.6f}")
    print(f"  Start Spatial ↔ Distance:   {analysis['start_spatial_distance_corr']:.6f}")
    print(f"  Goal Spatial ↔ Distance:    {analysis['goal_spatial_distance_corr']:.6f}")
    
    print("\n" + "="*90 + "\n")


def print_comparison_results(comparison: Dict, metric_name: str):
    """
    Print comparison results in a readable format.
    
    Args:
        comparison: Comparison results dictionary
        metric_name: Name of the metric (Time or Distance)
    """
    print(f"\n{'='*70}")
    print(f"{metric_name} Comparison: {comparison['test_1']} vs {comparison['test_2']}")
    print(f"{'='*70}")
    
    print(f"\n{comparison['test_1']}:")
    print(f"  Sample size: {comparison['sample_size_1']}")
    print(f"  Mean: {comparison['mean_1']:.4f}")
    print(f"  Std Dev: {comparison['std_1']:.4f}")
    print(f"  Median: {comparison['median_1']:.4f}")
    
    print(f"\n{comparison['test_2']}:")
    print(f"  Sample size: {comparison['sample_size_2']}")
    print(f"  Mean: {comparison['mean_2']:.4f}")
    print(f"  Std Dev: {comparison['std_2']:.4f}")
    print(f"  Median: {comparison['median_2']:.4f}")
    
    print(f"\nStatistical Tests (α = 0.05):")
    
    if 'mann_whitney_pvalue' in comparison:
        sig = "SIGNIFICANT" if comparison['mann_whitney_significant'] else "NOT SIGNIFICANT"
        print(f"  Mann-Whitney U test: p = {comparison['mann_whitney_pvalue']:.4f} ({sig})")
    
    if 'ks_pvalue' in comparison:
        sig = "SIGNIFICANT" if comparison['ks_significant'] else "NOT SIGNIFICANT"
        print(f"  Kolmogorov-Smirnov test: p = {comparison['ks_pvalue']:.4f} ({sig})")
    
    if 'levene_pvalue' in comparison:
        sig = "SIGNIFICANT" if comparison['levene_significant'] else "NOT SIGNIFICANT"
        print(f"  Levene's variance test: p = {comparison['levene_pvalue']:.4f} ({sig})")
    
    if 'fligner_pvalue' in comparison:
        sig = "SIGNIFICANT" if comparison['fligner_significant'] else "NOT SIGNIFICANT"
        print(f"  Fligner-Killeen variance test: p = {comparison['fligner_pvalue']:.4f} ({sig})")
    

    if 'brunner_munzel_pvalue' in comparison:
        sig = "SIGNIFICANT" if comparison['brunner_munzel_significant'] else "NOT SIGNIFICANT"
        print(f"  Brunner-Munzel test: p = {comparison['brunner_munzel_pvalue']:.4f} ({sig})")


def save_comparison_to_csv(comparison: Dict, output_csv: str):
    """
    Save comparison results to CSV file.
    
    Args:
        comparison: Comparison results dictionary
        output_csv: Path to output CSV file
    """
    with open(output_csv, 'w', newline='') as f:
        writer = csv.writer(f)
        
        # Write header and data rows
        for key, value in comparison.items():
            if isinstance(value, (int, float)):
                writer.writerow([key, value])
            else:
                writer.writerow([key, value])


def sum_distributions(data1: np.ndarray, data2: np.ndarray, 
                     method: str = 'pairwise') -> np.ndarray:
    """
    Sum two distributions to create a combined distribution.
    
    Args:
        data1: First data array
        data2: Second data array
        method: Method for summing distributions:
                - 'pairwise': Pairwise sum by index (run0+run0, run1+run1, etc.)
                  Introduces correlation, results in smaller variance.
                - 'convolution': All pairs (Cartesian product). Each value from data1 
                  combined with each value from data2. Results in n1*n2 samples.
                  Assumes independence, larger variance.
                - 'monte_carlo': Random resampling with replacement (n=min(len(data1), len(data2)))
                - 'bootstrap': Bootstrap resampling with replacement (n=max(len(data1), len(data2)))
        
    Returns:
        Array of summed values
    """
    if method == 'pairwise':
        # Pairwise summing by index - introduces correlation
        min_size = min(len(data1), len(data2))
        if len(data1) != len(data2):
            print(f"Warning: Sample sizes differ ({len(data1)} vs {len(data2)}). "
                  f"Using first {min_size} samples from each.", file=sys.stderr)
        return data1[:min_size] + data2[:min_size]
    
    elif method == 'convolution':
        # Cartesian product (all pairs) - assumes independence
        # Creates n1 * n2 samples
        result = []
        for val1 in data1:
            for val2 in data2:
                result.append(val1 + val2)
        return np.array(result)
    
    elif method == 'monte_carlo':
        # Monte Carlo: randomly sample from each distribution and sum
        # Uses smaller sample size
        n_samples = min(len(data1), len(data2))
        samples1 = np.random.choice(data1, size=n_samples, replace=True)
        samples2 = np.random.choice(data2, size=n_samples, replace=True)
        return samples1 + samples2
    
    elif method == 'bootstrap':
        # Bootstrap: resample both distributions independently with replacement
        # Uses larger sample size for better representation
        n_samples = max(len(data1), len(data2))
        samples1 = np.random.choice(data1, size=n_samples, replace=True)
        samples2 = np.random.choice(data2, size=n_samples, replace=True)
        return samples1 + samples2
    
    else:
        raise ValueError(f"Unknown method: {method}. Use 'pairwise', 'convolution', "
                        f"'monte_carlo', or 'bootstrap'")


def compare_summed_distributions(name1: str, data1: np.ndarray,
                                 name2: str, data2: np.ndarray,
                                 name_target: str, data_target: np.ndarray,
                                 metric_name: str, output_dir: str,
                                 sum_method: str = 'pairwise',
                                 no_display: bool = False) -> Dict:
    """
    Sum two distributions and compare the result to a target distribution.
    
    Args:
        name1: Name of first test type
        data1: Data from first test type
        name2: Name of second test type
        data2: Data from second test type
        name_target: Name of target test type
        data_target: Data from target test type
        metric_name: Name of the metric (Time or Distance)
        output_dir: Directory to save results
        sum_method: Method for summing distributions ('pairwise' or 'monte_carlo')
        no_display: If True, skip printing results
        
    Returns:
        Dictionary with comparison results
    """
    # Sum the first two distributions
    summed_data = sum_distributions(data1, data2, method=sum_method)
    summed_name = f"{name1}+{name2}"
    
    if not no_display:
        print(f"\n{'='*70}")
        print(f"Summed Distribution Analysis: {summed_name}")
        print(f"{'='*70}")
        print(f"Original samples: {name1}={len(data1)}, {name2}={len(data2)}")
        print(f"Summed samples: {len(summed_data)}")
        print(f"Summed mean: {np.mean(summed_data):.4f}")
        print(f"  (Expected if independent: {np.mean(data1) + np.mean(data2):.4f})")
        print(f"Summed std: {np.std(summed_data):.4f}")
        print(f"  (Expected if independent: {np.sqrt(np.var(data1) + np.var(data2)):.4f})")
    
    # Analyze summed distribution
    _, analysis_summed = analyze_distribution_fit(summed_data)
    
    # Analyze target distribution
    _, analysis_target = analyze_distribution_fit(data_target)
    
    if not no_display:
        print_distribution_analysis(summed_name, analysis_summed, metric_name)
        print_distribution_analysis(name_target, analysis_target, metric_name)
    
    # Compare summed vs target
    comparison = compare_two_distributions(
        summed_name, summed_data,
        name_target, data_target
    )
    
    # Add additional context about the sum
    comparison['sum_component_1'] = name1
    comparison['sum_component_2'] = name2
    comparison['sum_method'] = sum_method
    
    # Mean analysis
    comparison['mean_component_1'] = float(np.mean(data1))
    comparison['mean_component_2'] = float(np.mean(data2))
    comparison['expected_sum_mean'] = float(np.mean(data1) + np.mean(data2))
    comparison['actual_sum_mean'] = float(np.mean(summed_data))
    comparison['mean_difference'] = float(np.mean(summed_data) - np.mean(data_target))
    comparison['mean_difference_pct'] = float(
        100 * (np.mean(summed_data) - np.mean(data_target)) / np.mean(data_target)
        if np.mean(data_target) != 0 else 0
    )
    
    # Variance analysis
    var_component_1 = float(np.var(data1))
    var_component_2 = float(np.var(data2))
    var_target = float(np.var(data_target))
    actual_sum_var = float(np.var(summed_data))
    expected_sum_var = var_component_1 + var_component_2
    
    comparison['var_component_1'] = var_component_1
    comparison['var_component_2'] = var_component_2
    comparison['expected_sum_var'] = expected_sum_var
    comparison['actual_sum_var'] = actual_sum_var
    comparison['var_difference'] = float(actual_sum_var - var_target)
    comparison['var_difference_pct'] = float(
        100 * (actual_sum_var - var_target) / var_target
        if var_target != 0 else 0
    )
    
    if not no_display:
        print_comparison_results(comparison, metric_name)
        print(f"\nMean Comparison:")
        print(f"  {name1} mean: {comparison['mean_component_1']:.4f}")
        print(f"  {name2} mean: {comparison['mean_component_2']:.4f}")
        print(f"  Expected sum: {comparison['expected_sum_mean']:.4f}")
        print(f"  Actual sum: {comparison['actual_sum_mean']:.4f}")
        print(f"  Target ({name_target}): {comparison['mean_2']:.4f}")
        print(f"  Difference (sum - target): {comparison['mean_difference']:.4f} "
              f"({comparison['mean_difference_pct']:.2f}%)")
        
        print(f"\nVariance Comparison:")
        print(f"  {name1} variance: {comparison['var_component_1']:.4f}")
        print(f"  {name2} variance: {comparison['var_component_2']:.4f}")
        print(f"  Expected sum (var1 + var2): {comparison['expected_sum_var']:.4f}")
        print(f"  Actual sum variance: {comparison['actual_sum_var']:.4f}")
        print(f"  Target ({name_target}) variance: {var_target:.4f}")
        print(f"  Difference (sum - target): {comparison['var_difference']:.4f} "
              f"({comparison['var_difference_pct']:.2f}%)")
    
    # Save comparison results
    os.makedirs(output_dir, exist_ok=True)
    output_csv = os.path.join(
        output_dir,
        f"sum_comparison_{name1}+{name2}_vs_{name_target}_{metric_name.lower().replace(' ', '_').replace('(', '').replace(')', '')}.csv"
    )
    save_comparison_to_csv(comparison, output_csv)
    
    if not no_display:
        print(f"\nResults saved to {output_csv}")
    
    # Generate plots
    plot_summed = plot_distribution(summed_name, summed_data, analysis_summed, 
                                   metric_name, output_dir)
    plot_target = plot_distribution(name_target, data_target, analysis_target,
                                   metric_name, output_dir)
    plot_comp = plot_comparison(summed_name, summed_data, analysis_summed,
                               name_target, data_target, analysis_target,
                               metric_name, output_dir)
    
    if not no_display:
        print(f"Summed distribution plot: {plot_summed}")
        print(f"Target distribution plot: {plot_target}")
        print(f"Comparison plot: {plot_comp}")
    
    return comparison


def main():
    parser = argparse.ArgumentParser(
        description='Compare robot navigation tests using statistical distributions',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Extract metrics from two test types (time, distance only)
  python3 compare_navigation_tests.py -t /path/to/test_type_1 /path/to/test_type_2

  # Extract metrics including localization error (requires localization_error.csv in run folders)
  python3 compare_navigation_tests.py -t /path/to/test_type_1 /path/to/test_type_2 -m time distance loc_error_mean loc_error_var

  # Extract metrics from successful runs only (test.xml failures=0)
  python3 compare_navigation_tests.py -t /path/to/test_type_1 --successful-only -m time distance loc_error_mean
  
  # Compare extracted metrics
  python3 compare_navigation_tests.py -c test_type_1 test_type_2 -m time
  
  # Compare both time and localization error mean
  python3 compare_navigation_tests.py -c test_type_1 test_type_2 -m time loc_error_mean
  
  # Compare all metrics (time, distance, localization error mean and variance)
  python3 compare_navigation_tests.py -c test_type_1 test_type_2 -m time distance loc_error_mean loc_error_var
  
  # Sum two distributions and compare to a target (e.g., top-half + bottom-half vs full-map)
  # Try different methods if variance doesn't match:
  python3 compare_navigation_tests.py --sum test_top test_bottom test_full -m time distance
  python3 compare_navigation_tests.py --sum test_top test_bottom test_full --sum-method convolution -m time loc_error_mean
  python3 compare_navigation_tests.py --sum test_top test_bottom test_full --sum-method bootstrap -m loc_error_var

  # Analyze pose variance correlation within a single test type
  # Shows how variation in start/goal poses affects distance variance
  python3 compare_navigation_tests.py --pose-variance /path/to/test_type_geometric_1 -o outputs
  python3 compare_navigation_tests.py --pose-variance /path/to/test_type_geometric_1 /path/to/test_type_geometric_2 -o outputs

    # Compare variance of pose-position distribution vs variance of per-variant mean distances
    python3 compare_navigation_tests.py --pose-dist-variance /path/to/variant_1 /path/to/variant_2 /path/to/variant_3 -o outputs

    # Source-vs-others: compare pose differences to mean-distance differences
    # First argument is source variant, remaining arguments are comparison variants
    python3 compare_navigation_tests.py --pose-diff-vs-source /path/to/source_variant /path/to/other_variant_1 /path/to/other_variant_2 -o outputs
        """
    )
    
    parser.add_argument('-t', '--test-types', nargs='+', 
                       help='Paths to test type folders to extract metrics')
    parser.add_argument('-c', '--compare', nargs=2, 
                       help='Names of two test types to compare (from extracted metrics)')
    parser.add_argument('--sum', nargs=3, metavar=('TEST1', 'TEST2', 'TARGET'),
                       help='Sum TEST1 and TEST2 distributions, compare to TARGET')
    parser.add_argument('--sum-method', choices=['pairwise', 'convolution', 'monte_carlo', 'bootstrap'],
                       default='pairwise',
                       help='Method for summing distributions. pairwise: by index (smaller variance); '
                            'convolution: all pairs (larger variance, assumes independence); '
                            'monte_carlo: random resample (min size); bootstrap: resample (max size) '
                            '(default: pairwise)')
    parser.add_argument('-m', '--metrics', nargs='+', 
                       choices=['time', 'distance', 'loc_error_mean', 'loc_error_var'],
                       default=['time', 'distance'],
                       help='Metrics to compare: time, distance, loc_error_mean (mean covariance), '
                            'loc_error_var (variance of covariance)')
    parser.add_argument('--pose-variance', nargs='+',
                       help='Analyze pose variance correlation for one or more test types '
                            '(shows how pose variation affects distance variance within each test type)')
    parser.add_argument('--pose-dist-variance', nargs='+',
                       help='Compare distribution variance of all scenario.config start/goal positions '
                           'against distribution variance of per-variant mean traveled distances')
    parser.add_argument('--pose-diff-vs-source', nargs='+',
                       help='Source-vs-others comparison: first path is source variant, '
                           'remaining paths are comparison variants')
    parser.add_argument('-o', '--output-dir', default='navigation_comparison_results',
                       help='Output directory for results')
    parser.add_argument('--no-display', action='store_true',
                       help='Skip printing results to console')
    parser.add_argument('--successful-only', action='store_true',
                       help='During extraction, include only runs where test.xml has failures=0')
    
    args = parser.parse_args()
    
    # Check that at least one action is specified
    if (
        not args.test_types
        and not args.compare
        and not args.sum
        and not args.pose_variance
        and not args.pose_dist_variance
        and not args.pose_diff_vs_source
    ):
        parser.print_help()
        sys.exit(1)
    
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Extract metrics if test types are provided
    if args.test_types:
        # Determine if we need to extract localization error metrics
        extract_loc = any(m in ['loc_error_mean', 'loc_error_var'] for m in args.metrics)
        
        print(f"Extracting metrics from {len(args.test_types)} test type(s)...\n")
        
        for test_type_path in args.test_types:
            test_type_dir = Path(test_type_path)
            test_type_name = test_type_dir.name
            
            print(f"Processing {test_type_name}...")
            times, distances, loc_means, loc_vars = process_test_type(
                test_type_path, 
                successful_only=args.successful_only,
                extract_localization=extract_loc
            )
            
            if times and distances:
                print(f"  Successfully extracted {len(times)} runs")
                if extract_loc and loc_means:
                    print(f"  Extracted localization error metrics for {len(loc_means)} runs")
                save_metrics_to_csv(test_type_name, times, distances, args.output_dir,
                                  loc_means=loc_means if extract_loc else None,
                                  loc_vars=loc_vars if extract_loc else None)
            else:
                print(f"  No valid data found", file=sys.stderr)
    
    # Compare test types if requested
    if args.compare:
        test1_name, test2_name = args.compare
        
        for metric in args.metrics:
            metric_dir = args.output_dir
            
            if metric == 'time':
                file1 = os.path.join(metric_dir, f"{test1_name}_times.csv")
                file2 = os.path.join(metric_dir, f"{test2_name}_times.csv")
                metric_label = "Time (seconds)"
            elif metric == 'distance':
                file1 = os.path.join(metric_dir, f"{test1_name}_distances.csv")
                file2 = os.path.join(metric_dir, f"{test2_name}_distances.csv")
                metric_label = "Distance (meters)"
            elif metric == 'loc_error_mean':
                file1 = os.path.join(metric_dir, f"{test1_name}_loc_error_means.csv")
                file2 = os.path.join(metric_dir, f"{test2_name}_loc_error_means.csv")
                metric_label = "Mean Localization Error (meters)"
            else:  # loc_error_var
                file1 = os.path.join(metric_dir, f"{test1_name}_loc_error_vars.csv")
                file2 = os.path.join(metric_dir, f"{test2_name}_loc_error_vars.csv")
                metric_label = "Localization Error Variance (meters²)"
            
            if not os.path.exists(file1) or not os.path.exists(file2):
                print(f"Warning: Could not find metric files for {metric}", file=sys.stderr)
                continue
            
            # Read data
            df1 = pd.read_csv(file1)
            df2 = pd.read_csv(file2)
            
            data1 = df1.iloc[:, 1].values  # Second column
            data2 = df2.iloc[:, 1].values  # Second column
            
            # Analyze distributions
            _, analysis1 = analyze_distribution_fit(data1)
            _, analysis2 = analyze_distribution_fit(data2)
            
            if not args.no_display:
                print_distribution_analysis(test1_name, analysis1, metric_label)
                print_distribution_analysis(test2_name, analysis2, metric_label)
            
            # Compare distributions
            comparison = compare_two_distributions(
                test1_name, data1, test2_name, data2
            )
            
            if not args.no_display:
                print_comparison_results(comparison, metric_label)
            
            # Save comparison results
            output_csv = os.path.join(
                args.output_dir, 
                f"comparison_{test1_name}_vs_{test2_name}_{metric}.csv"
            )
            save_comparison_to_csv(comparison, output_csv)
            print(f"\nResults saved to {output_csv}")
            
            # Generate plots
            plot1_path = plot_distribution(test1_name, data1, analysis1, metric_label, args.output_dir)
            plot2_path = plot_distribution(test2_name, data2, analysis2, metric_label, args.output_dir)
            comp_plot_path = plot_comparison(test1_name, data1, analysis1, test2_name, data2, analysis2, metric_label, args.output_dir)
            
            print(f"Distribution plot for {test1_name}: {plot1_path}")
            print(f"Distribution plot for {test2_name}: {plot2_path}")
            print(f"Comparison plot: {comp_plot_path}")
    
    # Sum distributions and compare to target if requested
    if args.sum:
        name1, name2, name_target = args.sum
        
        for metric in args.metrics:
            metric_dir = args.output_dir
            
            if metric == 'time':
                file1 = os.path.join(metric_dir, f"{name1}_times.csv")
                file2 = os.path.join(metric_dir, f"{name2}_times.csv")
                file_target = os.path.join(metric_dir, f"{name_target}_times.csv")
                metric_label = "Time (seconds)"
            elif metric == 'distance':
                file1 = os.path.join(metric_dir, f"{name1}_distances.csv")
                file2 = os.path.join(metric_dir, f"{name2}_distances.csv")
                file_target = os.path.join(metric_dir, f"{name_target}_distances.csv")
                metric_label = "Distance (meters)"
            elif metric == 'loc_error_mean':
                file1 = os.path.join(metric_dir, f"{name1}_loc_error_means.csv")
                file2 = os.path.join(metric_dir, f"{name2}_loc_error_means.csv")
                file_target = os.path.join(metric_dir, f"{name_target}_loc_error_means.csv")
                metric_label = "Mean Localization Error (meters)"
            else:  # loc_error_var
                file1 = os.path.join(metric_dir, f"{name1}_loc_error_vars.csv")
                file2 = os.path.join(metric_dir, f"{name2}_loc_error_vars.csv")
                file_target = os.path.join(metric_dir, f"{name_target}_loc_error_vars.csv")
                metric_label = "Localization Error Variance (meters²)"
            
            if not os.path.exists(file1) or not os.path.exists(file2) or not os.path.exists(file_target):
                print(f"Warning: Could not find all required metric files for {metric}", file=sys.stderr)
                print(f"  Need: {file1}, {file2}, {file_target}", file=sys.stderr)
                continue
            
            # Read data
            df1 = pd.read_csv(file1)
            df2 = pd.read_csv(file2)
            df_target = pd.read_csv(file_target)
            
            data1 = df1.iloc[:, 1].values  # Second column
            data2 = df2.iloc[:, 1].values  # Second column
            data_target = df_target.iloc[:, 1].values  # Second column
            
            # Sum distributions and compare to target
            compare_summed_distributions(
                name1, data1,
                name2, data2,
                name_target, data_target,
                metric_label,
                args.output_dir,
                sum_method=args.sum_method,
                no_display=args.no_display
            )
    
    # Analyze pose variance correlation if requested
    if args.pose_variance:
        print(f"Analyzing pose variance correlation for {len(args.pose_variance)} test type(s)...\n")
        
        for test_type_path in args.pose_variance:
            test_type_dir = Path(test_type_path)
            test_type_name = test_type_dir.name
            
            print(f"Processing {test_type_name}...")
            
            analysis = analyze_pose_variance_correlation(test_type_path)
            
            if analysis:
                if not args.no_display:
                    print_pose_variance_analysis(test_type_name, analysis)
                
                # Generate plot
                plot_path = plot_pose_variance_correlation(test_type_name, analysis, args.output_dir)
                print(f"  Pose variance plot saved to: {plot_path}")
            else:
                print(f"  Failed to analyze pose variance for {test_type_name}", file=sys.stderr)

    # Compare variance of pose-position distribution vs variance of distance-mean distribution
    if args.pose_dist_variance:
        print(f"Analyzing pose-position distribution vs distance-mean distribution for {len(args.pose_dist_variance)} variant(s)...\n")
        analysis = analyze_pose_distribution_vs_distance_means(args.pose_dist_variance)

        if analysis:
            if not args.no_display:
                print_pose_distribution_vs_distance_means_analysis(analysis)

            plot_path = plot_pose_distribution_vs_distance_means(analysis, args.output_dir)
            print(f"  Pose distribution vs distance means plot saved to: {plot_path}")
        else:
            print("  Failed to analyze pose-position distribution vs distance means (need at least 2 valid variants)", file=sys.stderr)

    # Source-vs-others pose-difference vs distance-mean-difference analysis
    if args.pose_diff_vs_source:
        if len(args.pose_diff_vs_source) < 3:
            print(
                "Error: --pose-diff-vs-source requires at least 3 paths: source + at least 2 comparison variants",
                file=sys.stderr,
            )
        else:
            source_variant = args.pose_diff_vs_source[0]
            other_variants = args.pose_diff_vs_source[1:]

            print(
                f"Analyzing source-vs-others pose/distance differences: source=\"{Path(source_variant).name}\", "
                f"comparisons={len(other_variants)}...\n"
            )

            analysis = analyze_source_pose_vs_distance_differences(source_variant, other_variants)

            if analysis:
                if not args.no_display:
                    print_source_pose_vs_distance_differences_analysis(analysis)

                plot_path = plot_source_pose_vs_distance_differences(analysis, args.output_dir)
                print(f"  Source-vs-others pose/distance diff plot saved to: {plot_path}")
            else:
                print(
                    "  Failed source-vs-others analysis (need 1 valid source and at least 2 valid comparison variants)",
                    file=sys.stderr,
                )


if __name__ == '__main__':
    main()
