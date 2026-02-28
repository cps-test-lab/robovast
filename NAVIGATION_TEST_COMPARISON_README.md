# Navigation Test Statistical Comparison Tool

A Python script for probabilistic comparison of robot navigation test results using distribution fitting and nonparametric statistical tests.

## Features

- **Metric Extraction**: Automatically extracts time taken and distance traveled from `poses.csv` files in test runs
- **Distribution Fitting**: Tests data against 6 distributions (normal, exponential, lognormal, gamma, weibull, poisson)
- **Visualization**: Generates distribution plots with best-fit curves and comparison overlays for visual analysis
- **Comprehensive Statistical Tests**: 
  - Mann-Whitney U test (tests if distributions differ)
  - Kolmogorov-Smirnov test (tests if distributions differ)
  - Levene's test (tests if variances are equal)
  - Fligner-Killeen test (alternative variance test)
  - Brunner-Munzel test (robust alternative to Mann-Whitney)
- **Detailed Analysis**: Provides distribution parameters, skewness, kurtosis, and goodness-of-fit metrics
- **CSV Output**: Saves extracted metrics and comparison results for further analysis

## Installation

The script requires Python 3 and the following packages:
```bash
pip install numpy pandas scipy
```

These are typically already available in most Python environments.

## Usage

### Basic Usage

```bash
python3 compare_navigation_tests.py -h
```

### 1. Extract Metrics from Test Types

Extract time and distance metrics from one or more test type folders:

```bash
python3 compare_navigation_tests.py \
  -t /path/to/test_type_1 /path/to/test_type_2 \
  -o results_output_dir
```

This will:
- Scan each test type folder for numbered run subfolders (0, 1, 2, etc.)
- Extract `poses.csv` from each successful run
- Calculate total time (from first to last timestamp) and total distance (sum of pose changes)
- Save results to `{test_type_name}_times.csv` and `{test_type_name}_distances.csv`

**Example with your data:**
```bash
python3 compare_navigation_tests.py \
  -t results/run-2026-02-22-141158/rooms10m2o-1-4-1-1 \
     results/run-2026-02-22-141158/office20m4o-1-21-1-2 \
  -o comparison_results
```

### 2. Compare Two Test Types

After extracting metrics, compare two test types statistically:

```bash
python3 compare_navigation_tests.py \
  -c test_type_1 test_type_2 \
  -m time distance \
  -o comparison_results
```

Options for `-m`:
- `time`: Compare execution time
- `distance`: Compare distance traveled
- `time distance`: Compare both (default)

This will:
1. Load the extracted metrics for both test types
2. Analyze which distribution fits each dataset best
3. Run multiple nonparametric statistical tests
4. Display results and save to CSV files

**Example:**
```bash
python3 compare_navigation_tests.py \
  -c rooms10m2o-1-4-1-1 office20m4o-1-21-1-2 \
  -m time distance \
  -o comparison_results
```

### 3. Combined Extraction and Comparison

Extract metrics and immediately compare them:

```bash
# Extract metrics
python3 compare_navigation_tests.py \
  -t /path/to/test_type_1 /path/to/test_type_2 \
  -o comparison_results

# Then compare
python3 compare_navigation_tests.py \
  -c test_type_1 test_type_2 \
  -o comparison_results
```

### Command Line Options

```
-h, --help                Show help message and exit

-t, --test-types          Paths to test type folders to extract metrics
                          (space-separated list)

-c, --compare             Names of two test types to compare 
                          (from extracted metrics)

-m, --metrics             Metrics to compare: time, distance, or both
                          Default: time distance

-o, --output-dir          Output directory for results
                          Default: navigation_comparison_results

--no-display              Skip printing results to console
                          (results still saved to CSV)
```

## Output Files

### Extracted Metrics
- `{test_type_name}_times.csv`: Time in seconds for each successful run
- `{test_type_name}_distances.csv`: Distance in meters for each successful run

Example:
```
run_index,time_seconds
0,33.756
1,33.591
```

### Comparison Results
- `comparison_{test1}_vs_{test2}_time.csv`: Time comparison statistics
- `comparison_{test1}_vs_{test2}_distance.csv`: Distance comparison statistics
- `distribution_{test_type}_{metric}.png`: Individual distribution plots with best-fit curve
- `comparison_{test1}_vs_{test2}_{metric}.png`: Side-by-side comparison with overlaid best-fit distributions

Example comparison CSV:
```
test_1,test_type_1
test_2,test_type_2
sample_size_1,2
sample_size_2,3
mean_1,33.6735
mean_2,50.4650
...
mann_whitney_pvalue,0.2
ks_pvalue,0.2
levene_pvalue,0.293
...
```
mean_2,50.4650
...
mann_whitney_pvalue,0.2
ks_pvalue,0.2
levene_pvalue,0.293
...
```

## Understanding the Output

### Distribution Analysis Section

Each test type gets a distribution analysis that shows:
- **Sample statistics**: Mean, standard deviation, median, min, max
- **Shape statistics**: Skewness (asymmetry) and kurtosis (tail heaviness)
- **Normality Tests**: Two tests check if data is normally distributed:
  - **Shapiro-Wilk test**: Best for small samples (n < 50)
    - p > 0.05: Data appears normal
    - p ≤ 0.05: Data is not normally distributed
  - **Jarque-Bera test**: Tests based on skewness and kurtosis
    - p > 0.05: Data appears normal
    - p ≤ 0.05: Data is not normally distributed
- **Best fit distribution**: The distribution that best matches the data
- **Distribution fit statistics**: Kolmogorov-Smirnov test results for each distribution

### Comparison Section

Statistical tests compare two test types:

1. **Mann-Whitney U test** (nonparametric alternative to t-test)
   - Tests if the distributions are different
   - More robust than parametric tests, doesn't assume normality

2. **Kolmogorov-Smirnov test** (distribution comparison)
   - Tests if two distributions are different
   - Sensitive to differences anywhere in the distribution

3. **Levene's test** (variance comparison)
   - Tests if variability is the same between groups
   - p > 0.05: Similar variance
   - p ≤ 0.05: Different variance

4. **Fligner-Killeen test** (nonparametric variance test)
   - Alternative to Levene's that doesn't assume normality

5. **Brunner-Munzel test** (robust alternative to Mann-Whitney)
   - Uses ranks like Mann-Whitney but more robust

### Interpreting Results

**Significant Difference (p ≤ 0.05):**
- Mann-Whitney U significant: The distributions differ
- Levene's significant: Different variability between test types
- Multiple tests significant: Strong evidence of difference

**No Significant Difference (p > 0.05):**
- Not enough evidence to conclude the test types differ
- They may have similar performance characteristics

## Example Workflow

```bash
# 1. Examine your test results directory
ls /path/to/results/run-YYYY-MM-DD-HHMMSS/

# 2. Identify test types to compare
# Example: rooms10m2o-1-4-1-1, office20m4o-1-21-1-2, etc.

# 3. Extract metrics from multiple test types
python3 compare_navigation_tests.py \
  -t /path/to/results/test_type_A \
     /path/to/results/test_type_B \
     /path/to/results/test_type_C \
  -o my_results

# 4. Compare pairs of test types
python3 compare_navigation_tests.py \
  -c test_type_A test_type_B \
  -m time distance \
  -o my_results

python3 compare_navigation_tests.py \
  -c test_type_B test_type_C \
  -m time distance \
  -o my_results

# 5. Review results
cat my_results/comparison_test_type_A_vs_test_type_B_time.csv
```

## Data Format

The script expects:
- Test type folders containing numbered run subfolders (0, 1, 2, ...)
- Each run folder may contain a `poses.csv` file
- Runs without `poses.csv` are skipped (unsuccessful runs)

### Expected CSV Structure for poses.csv:
```csv
frame,timestamp,position.x,position.y,position.z,orientation.roll,orientation.pitch,orientation.yaw
nav2_turtlebot4_base_link_gt,3.657,-4.249980402373631,2.699999999999742,-0.004449701291065948,...
...
```

The script extracts:
- **Total Time**: Last timestamp - First timestamp
- **Total Distance**: Sum of Euclidean distances between consecutive poses for x, y, z positions

## Tips and Tricks

1. **Large Datasets**: If you have many test runs, the sample size increases statistical power
   - Small samples (n < 5): Results may be unreliable
   - Medium samples (n = 5-20): Reasonable confidence
   - Large samples (n > 20): High statistical power

2. **Interpreting Small Samples**: The Shapiro-Wilk test is unreliable with very small samples (n < 3)
   - With n=2, p-value will be NaN
   - Use visual inspection and multiple tests

3. **Multiple Comparisons**: If comparing many test type pairs, consider:
   - Bonferroni correction: Use α = 0.05/number_of_comparisons
   - Or interpret results as exploratory

4. **Effect Size**: Look at mean differences:
   - Large mean difference with non-significant test: May need more samples
   - Small mean difference: Likely not practically significant

5. **View Results in Spreadsheet**: Open CSV files in Excel/LibreOffice Calc for easy viewing:
   ```bash
   libreoffice my_results/comparison_*.csv &
   ```

## Troubleshooting

**"Could not find metric files":**
- Make sure you extractedmetrics first: use `-t` flag
- Check that test type names match exactly (case-sensitive)

**"No valid data found":**
- Test runs don't have `poses.csv` files
- The runs may have failed or not completed
- Check that input paths point to test type folders, not individual run folders

**"Sample size too small" or "NaN values":**
- Analysis may be unreliable with < 3 samples
- Extract metrics from more test runs if possible
- Still valid to compare means and distributions visually

