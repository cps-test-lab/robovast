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
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import warnings

import numpy as np
import pandas as pd
from scipy import stats
from scipy.stats import (
    kstest, normaltest, shapiro, anderson, jarque_bera,
    gamma, expon, weibull_min, lognorm, norm
)

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


def process_test_type(test_type_path: str) -> Tuple[List[float], List[float]]:
    """
    Process all runs in a test type folder and extract metrics.
    
    Args:
        test_type_path: Path to the test type folder containing run subfolders
        
    Returns:
        Tuple of (times_list, distances_list)
    """
    times = []
    distances = []
    
    test_type_dir = Path(test_type_path)
    
    # Iterate through run folders (0, 1, 2, ...)
    for run_dir in sorted(test_type_dir.iterdir()):
        if not run_dir.is_dir() or run_dir.name.startswith('_'):
            continue
        
        poses_csv = run_dir / 'poses.csv'
        
        if poses_csv.exists():
            result = extract_pose_metrics(str(poses_csv))
            if result is not None:
                time_taken, distance_traveled = result
                times.append(time_taken)
                distances.append(distance_traveled)
                print(f"  Run {run_dir.name}: time={time_taken:.2f}s, distance={distance_traveled:.2f}m")
            else:
                print(f"  Run {run_dir.name}: Failed to process", file=sys.stderr)
        else:
            print(f"  Run {run_dir.name}: No poses.csv found (skipped)")
    
    return times, distances


def save_metrics_to_csv(test_type_name: str, times: List[float], 
                       distances: List[float], output_dir: str) -> Tuple[str, str]:
    """
    Save time and distance metrics to separate CSV files.
    
    Args:
        test_type_name: Name of the test type
        times: List of time values
        distances: List of distance values
        output_dir: Directory to save CSV files
        
    Returns:
        Tuple of (time_csv_path, distance_csv_path)
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
    
    return time_csv_path, distance_csv_path


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
        'weibull': weibull_min
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
        'skewness': float(stats.skew(data)),
        'kurtosis': float(stats.kurtosis(data)),
    }
    
    # Test for normality
    try:
        _, norm_pval = shapiro(data)
        analysis['shapiro_pvalue'] = norm_pval
        analysis['is_normal'] = norm_pval > 0.05
    except:
        analysis['is_normal'] = False
    
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
    
    # Mann-Whitney U test (tests if distributions are different)
    try:
        u_stat, u_pval = stats.mannwhitneyu(data1, data2, alternative='two-sided')
        results['mann_whitney_u'] = float(u_stat)
        results['mann_whitney_pvalue'] = float(u_pval)
        results['mann_whitney_significant'] = u_pval < 0.05
    except:
        pass
    
    # Kolmogorov-Smirnov test (tests if distributions are different)
    try:
        ks_stat, ks_pval = stats.ks_2samp(data1, data2)
        results['ks_statistic'] = float(ks_stat)
        results['ks_pvalue'] = float(ks_pval)
        results['ks_significant'] = ks_pval < 0.05
    except:
        pass
    
    # Levene's test (tests if variances are equal)
    try:
        levene_stat, levene_pval = stats.levene(data1, data2)
        results['levene_statistic'] = float(levene_stat)
        results['levene_pvalue'] = float(levene_pval)
        results['levene_significant'] = levene_pval < 0.05
    except:
        pass
    
    # Fligner-Killeen test (alternative variance test)
    try:
        fk_stat, fk_pval = stats.fligner(data1, data2)
        results['fligner_statistic'] = float(fk_stat)
        results['fligner_pvalue'] = float(fk_pval)
        results['fligner_significant'] = fk_pval < 0.05
    except:
        pass
    
    # Mood's median test
    try:
        med_stat, med_pval, med_med, med_cont = stats.median_test(data1, data2)
        results['mood_median_statistic'] = float(med_stat)
        results['mood_median_pvalue'] = float(med_pval)
        results['mood_median_significant'] = med_pval < 0.05
    except:
        pass
    
    # Brunner-Munzel test (alternative to Mann-Whitney)
    try:
        bm_stat, bm_pval = stats.brunnermunzel(data1, data2)
        results['brunner_munzel_statistic'] = float(bm_stat)
        results['brunner_munzel_pvalue'] = float(bm_pval)
        results['brunner_munzel_significant'] = bm_pval < 0.05
    except:
        pass
    
    return results


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
    
    if analysis.get('is_normal'):
        print(f"Shapiro-Wilk p-value: {analysis['shapiro_pvalue']:.4f} (Normal)")
    else:
        print(f"Shapiro-Wilk p-value: {analysis['shapiro_pvalue']:.4f} (Not Normal)")
    
    print(f"\nBest fit distribution: {analysis['best_fit_distribution']}")
    
    print("\nDistribution Fit Statistics:")
    for dist_name, fit_info in analysis['distribution_fits'].items():
        print(f"  {dist_name}:")
        if 'fit_stats' in fit_info and 'ks_pvalue' in fit_info['fit_stats']:
            ks_pval = fit_info['fit_stats']['ks_pvalue']
            print(f"    KS Test p-value: {ks_pval:.4f}")


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
    
    if 'mood_median_pvalue' in comparison:
        sig = "SIGNIFICANT" if comparison['mood_median_significant'] else "NOT SIGNIFICANT"
        print(f"  Mood's median test: p = {comparison['mood_median_pvalue']:.4f} ({sig})")
    
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


def main():
    parser = argparse.ArgumentParser(
        description='Compare robot navigation tests using statistical distributions',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Extract metrics from two test types
  python3 compare_navigation_tests.py -t /path/to/test_type_1 /path/to/test_type_2
  
  # Compare extracted metrics
  python3 compare_navigation_tests.py -c test_type_1 test_type_2 -m time
  
  # Compare both time and distance
  python3 compare_navigation_tests.py -c test_type_1 test_type_2 -m time distance
        """
    )
    
    parser.add_argument('-t', '--test-types', nargs='+', 
                       help='Paths to test type folders to extract metrics')
    parser.add_argument('-c', '--compare', nargs=2, 
                       help='Names of two test types to compare (from extracted metrics)')
    parser.add_argument('-m', '--metrics', nargs='+', choices=['time', 'distance'],
                       default=['time', 'distance'],
                       help='Metrics to compare (time, distance, or both)')
    parser.add_argument('-o', '--output-dir', default='navigation_comparison_results',
                       help='Output directory for results')
    parser.add_argument('--no-display', action='store_true',
                       help='Skip printing results to console')
    
    args = parser.parse_args()
    
    # Check that at least one action is specified
    if not args.test_types and not args.compare:
        parser.print_help()
        sys.exit(1)
    
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Extract metrics if test types are provided
    if args.test_types:
        print(f"Extracting metrics from {len(args.test_types)} test type(s)...\n")
        
        for test_type_path in args.test_types:
            test_type_dir = Path(test_type_path)
            test_type_name = test_type_dir.name
            
            print(f"Processing {test_type_name}...")
            times, distances = process_test_type(test_type_path)
            
            if times and distances:
                print(f"  Successfully extracted {len(times)} runs")
                save_metrics_to_csv(test_type_name, times, distances, args.output_dir)
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
            else:  # distance
                file1 = os.path.join(metric_dir, f"{test1_name}_distances.csv")
                file2 = os.path.join(metric_dir, f"{test2_name}_distances.csv")
                metric_label = "Distance (meters)"
            
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


if __name__ == '__main__':
    main()
