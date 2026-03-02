# Navigation Test Statistical Comparison Tool

Python tool to extract and compare navigation metrics from RoboVAST result folders.

The script supports:
- metric extraction (`-t`)
- statistical comparison (`-c`)
- summed-distribution comparison (`--sum`)
- standard source-vs-dependents distribution comparison (`--standard-compare`)
- pose variance analysis across variants (`--pose-variance`)
- pose-position distribution vs distance-mean distribution (`--pose-dist-variance`)
- source-vs-others pose/distance difference correlation (`--pose-diff-vs-source`)

## Dependencies

```bash
pip install numpy pandas scipy matplotlib pyyaml
```

## Command Reference

```text
-t, --test-types PATH [PATH ...]
    Extract metrics from test type folders

--auto-variants PARENT_DIR
    Automatically discover and extract metrics from all variant subdirectories in PARENT_DIR
    (detects directories containing numbered run subdirectories like 0/, 1/, 2/, etc.)

--list-variants PARENT_DIR
    List all available variants in a directory (shows variant names and run counts)

--standard-compare SOURCE DEPENDENT [DEPENDENT ...]
  Standard comparison mode: first variant is source, remaining are dependent variants
  Uses -i as parent directory containing variant folders; outputs plots/summary CSVs/TXTs to -o

--no-histograms
  Skip histograms in standard comparison plots (show only distribution curves for cleaner visualization)

-c, --compare TEST1 TEST2
    Compare two extracted test types by name (name = folder basename used in CSV filenames)

-i, --input-dir DIR
  Input directory
    - for -c: directory containing extracted metric CSV files
    - for --standard-compare and --sum: parent directory containing variant folders
  (default: same as --output-dir)

--sum VARIANT1 [VARIANT2 ...] TARGET_VARIANT
    Sum all but the last variant and compare with the target (last variant)
    Requires -i as parent directory containing variant folders

--sum-method {pairwise,convolution,monte_carlo,bootstrap}
    Summation strategy (default: pairwise)

-m, --metrics {time,distance,loc_error_mean,loc_error_var} [...]
    Metrics to process (default: time distance)

--pose-variance PATH [PATH ...]
    Analyze pose variance correlation for each provided test type base

--pose-dist-variance PATH [PATH ...]
    Compare variance of:
      1) all start/goal positions (from scenario.config across provided variants)
      2) per-variant mean traveled distance distribution

--pose-diff-vs-source SOURCE OTHER [OTHER ...]
    Compare source-vs-others pose differences against mean-distance differences

--pose-diff-method {nearest,centroid}
  Pose-difference method for --pose-diff-vs-source:
  nearest = source→target nearest-neighbor pose-set distance (default)
  centroid = legacy centroid/pairwise-goal method

--successful-only
    During extraction only, keep runs with test.xml failures=0

--no-display
    Skip verbose console printing (files/plots still generated)

-o, --output-dir DIR
    Output folder (default: navigation_comparison_results)
```

---

## Concrete Copy/Paste Examples (run-2026-02-28-030930)

All examples below are ready to paste directly into a shell.

### 1) Extract metrics (`-t`)

```bash
python3 compare_navigation_tests.py -t \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-1 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-2 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-3 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-4 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-5 \
  -o semantic_area_sampling_outputs
```

### 1b) List available variants (`--list-variants`)

Before extracting, you can see what variants are available:

```bash
python3 compare_navigation_tests.py --list-variants \
  results/run-2026-02-28-030930/
```

Output:
```
Variants found in results/run-2026-02-28-030930/:

Variant Name                                       Runs   Path
------------------------------------------------------------
mt-semantic-area-sampling-1-1                      10     results/run-2026-02-28-030930/mt-semantic-area-sampling-1-1
mt-semantic-area-sampling-1-2                      10     results/run-2026-02-28-030930/mt-semantic-area-sampling-1-2
mt-semantic-area-sampling-1-3                      10     results/run-2026-02-28-030930/mt-semantic-area-sampling-1-3
mt-semantic-area-sampling-1-4                      10     results/run-2026-02-28-030930/mt-semantic-area-sampling-1-4
mt-semantic-area-sampling-1-5                      10     results/run-2026-02-28-030930/mt-semantic-area-sampling-1-5

Total: 5 variant(s)
```

### 1c) Auto-discover and extract from all variants (`--auto-variants`)

Instead of listing each variant path individually, you can provide a parent directory and the script will automatically discover all variant subdirectories:

```bash
python3 compare_navigation_tests.py --auto-variants \
  results/run-2026-02-28-030930/ \
  -o semantic_area_sampling_outputs
```

### 1d) Auto-discover with additional explicit variants

You can also combine auto-discovery with explicit paths:

```bash
python3 compare_navigation_tests.py --auto-variants \
  results/run-2026-02-28-030930/ \
  -t /path/to/other_variant \
  -o semantic_area_sampling_outputs
```

### 2) Extract metrics from successful runs only (`--successful-only`)

```bash
python3 compare_navigation_tests.py -t \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-1 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-2 \
  --successful-only \
  -o semantic_area_sampling_outputs_success_only
```

### 3) Extract including localization metrics (`-m loc_error_mean loc_error_var`)

```bash
python3 compare_navigation_tests.py -t \
  results/run-2026-02-28-030930/mt-remove-by-area-1-1-1 \
  results/run-2026-02-28-030930/mt-remove-by-area-1-1-2 \
  -m time distance loc_error_mean loc_error_var \
  -o remove_by_area_outputs
```

### 3b) Auto-discover with localization metrics

```bash
python3 compare_navigation_tests.py --auto-variants \
  results/run-2026-02-28-030930/ \
  -m time distance loc_error_mean loc_error_var \
  -o remove_by_area_outputs
```

### 4) Compare two extracted test types (`-c`)

Note: `-c` uses test type names (folder basenames), not paths.

```bash
python3 compare_navigation_tests.py -c \
  mt-semantic-area-sampling-1-1 mt-semantic-area-sampling-1-2 \
  -m time distance \
  -o semantic_area_sampling_outputs
```

### 4b) Compare with separate input and output directories

Extract metrics to one location, then run comparisons with results saved elsewhere:

```bash
# Extract metrics to extraction_results/
python3 compare_navigation_tests.py --auto-variants \
  results/run-2026-02-28-030930/ \
  -o extraction_results

# Compare with input from extraction_results/, output to comparison_results/
python3 compare_navigation_tests.py -c \
  mt-semantic-area-sampling-1-1 mt-semantic-area-sampling-1-2 \
  -i extraction_results \
  -o comparison_results \
  -m time distance
```

### 5) Compare localization-error metrics after extraction

```bash
python3 compare_navigation_tests.py -c \
  mt-remove-by-area-1-1-1 mt-remove-by-area-1-1-2 \
  -m loc_error_mean loc_error_var \
  -o remove_by_area_outputs
```

### 6) Sum distributions and compare to target (`--sum`)

Sum the first N-1 variants and compare to the last variant. Requires `-i` as parent directory containing variant folders.

```bash
python3 compare_navigation_tests.py --sum \
  mt-remove-by-area-1-1-2 mt-remove-by-area-1-1-3 mt-remove-by-area-1-1-1 \
  -i results/run-2026-02-28-030930 \
  -o sum_comparison_results \
  -m time distance
```

What this does:
- Sums variants `mt-remove-by-area-1-1-2 + mt-remove-by-area-1-1-3`
- Compares the sum to target variant `mt-remove-by-area-1-1-1`
- Uses specified summing method (default: pairwise)
- Generates plots + CSV/text summaries (like standard-compare)
- Saves `command.txt` for easy regeneration

### 6b) Sum with localization error metrics

```bash
python3 compare_navigation_tests.py --sum \
  mt-remove-by-area-1-1-2 mt-remove-by-area-1-1-3 mt-remove-by-area-1-1-1 \
  -i results/run-2026-02-28-030930 \
  -o sum_comparison_results \
  -m time distance loc_error_mean loc_error_var
```

### 7) Sum with each method (`--sum-method`)

The `--sum-method` argument controls how distributions are combined:

| Method | Behavior | Variance | Use Case |
|--------|----------|----------|----------|
| `pairwise` (default) | Pairs by index: run0+run0, run1+run1, ... | Smaller (correlated) | Testing combined execution with paired runs |
| `convolution` | All pairs (Cartesian product): each value from first combined with each from second | Larger (independent) | Testing all possible combinations |
| `monte_carlo` | Random resampling with replacement (n = min size) | Variable | Quick approximate variance |
| `bootstrap` | Bootstrap resampling with replacement (n = max size) | Variable | Conservative variance estimate |

#### pairwise (default)
```bash
python3 compare_navigation_tests.py --sum \
  mt-remove-by-area-1-1-2 mt-remove-by-area-1-1-3 mt-remove-by-area-1-1-1 \
  -i results/run-2026-02-28-030930 \
  --sum-method pairwise \
  -m distance \
  -o remove_by_area_outputs
```

#### convolution
```bash
python3 compare_navigation_tests.py --sum \
  mt-remove-by-area-1-1-2 mt-remove-by-area-1-1-3 mt-remove-by-area-1-1-1 \
  -i results/run-2026-02-28-030930 \
  --sum-method convolution \
  -m distance \
  -o remove_by_area_outputs
```

#### Monte Carlo
```bash
python3 compare_navigation_tests.py --sum \
  mt-remove-by-area-1-1-2 mt-remove-by-area-1-1-3 mt-remove-by-area-1-1-1 \
  -i results/run-2026-02-28-030930 \
  --sum-method monte_carlo \
  -m time distance \
  -o remove_by_area_outputs
```

#### Bootstrap
```bash
python3 compare_navigation_tests.py --sum \
  mt-remove-by-area-1-1-2 mt-remove-by-area-1-1-3 mt-remove-by-area-1-1-1 \
  -i results/run-2026-02-28-030930 \
  --sum-method bootstrap \
  -m loc_error_var \
  -o remove_by_area_outputs
```

### 8) Standard comparison: source vs dependents (`--standard-compare`)

Example: source variant `1-1` compared against dependent variants `1-2` and `1-3` for `time` and `distance`.

```bash
python3 compare_navigation_tests.py --standard-compare \
  mt-semantic-area-sampling-1-1 mt-semantic-area-sampling-1-2 mt-semantic-area-sampling-1-3 \
  -i results/run-2026-02-28-030930 \
  -o semantic_area_sampling_outputs \
  -m time distance
```

What this does:
- Fits the best distribution for each selected metric and each variant
- Prints per-variant fit analysis (same style as existing comparison output)
- Prints source-relative percentage differences for mean and variance
- Generates one combined plot per metric with all variant fits
- Plot labels include distribution fit p-value (higher = better fit) for quick visual assessment
- Improved descriptive plot titles, e.g., "Distance travelled during execution distribution per variant"
- Saves a per-metric summary CSV and summary text file (same as console output)

#### 8a) With histograms (default, shows distribution curves + step histograms):

```bash
python3 compare_navigation_tests.py --standard-compare \
  mt-semantic-area-sampling-1-1 mt-semantic-area-sampling-1-2 mt-semantic-area-sampling-1-3 \
  -i results/run-2026-02-28-030930 \
  -o semantic_area_sampling_outputs \
  -m time distance
```

#### 8b) Without histograms (cleaner visualization with only distribution curves):

```bash
python3 compare_navigation_tests.py --standard-compare \
  mt-semantic-area-sampling-1-1 mt-semantic-area-sampling-1-2 mt-semantic-area-sampling-1-3 \
  -i results/run-2026-02-28-030930 \
  -o semantic_area_sampling_outputs \
  -m time distance \
  --no-histograms
```

#### Label Format

Plot curve labels now include:
- Variant name
- **Distribution type with p-value** (e.g., `[weibull p=0.984]`) - assess fit quality at a glance
- Mean (μ) and variance (σ²)

Example label: `mt-add-1g [weibull p=0.984] μ=386.768, σ²=846.417`

### 9) Pose variance analysis across variants (`--pose-variance`)

```bash
compare_navigation_tests.py --pose-variance \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-1 \
  -o semantic_area_sampling_outputs
```

### 10) Pose-position distribution vs distance-mean distribution (`--pose-dist-variance`)

```bash
python3 compare_navigation_tests.py --pose-dist-variance \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-1 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-2 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-3 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-4 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-5 \
  -o semantic_area_sampling_outputs
```

### 11) Source-vs-others pose/distance difference correlation (`--pose-diff-vs-source`)

```bash
python3 compare_navigation_tests.py --pose-diff-vs-source \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-1 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-2 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-3 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-4 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-5 \
  -o semantic_area_sampling_outputs
```

### 11b) Choose pose-difference method (`--pose-diff-method`)

Nearest-neighbor pose-set method (default):

```bash
python3 compare_navigation_tests.py --pose-diff-vs-source \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-1 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-2 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-3 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-4 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-5 \
  --pose-diff-method nearest \
  -o semantic_area_sampling_outputs
```

Centroid legacy method:

```bash
python3 compare_navigation_tests.py --pose-diff-vs-source \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-1 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-2 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-3 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-4 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-5 \
  --pose-diff-method centroid \
  -o semantic_area_sampling_outputs
```

### 12) Quiet mode for long runs (`--no-display`)

```bash
python3 compare_navigation_tests.py --pose-diff-vs-source \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-1 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-2 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-3 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-4 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-5 \
  -o semantic_area_sampling_outputs --no-display
```

---

## Output Files

### Extraction
- `{test_type_name}_times.csv`
- `{test_type_name}_distances.csv`
- `{test_type_name}_loc_error_means.csv` (if requested)
- `{test_type_name}_loc_error_vars.csv` (if requested)

### Pairwise compare (`-c`)
- `comparison_{test1}_vs_{test2}_{metric}.csv`
- `distribution_{test_type}_{metric}.png`
- `comparison_{test1}_vs_{test2}_{metric}.png`

### Sum compare (`--sum`)
- `standard_comparison_{metric}.png`
- `standard_compare_{metric}.csv`
- `standard_compare_{metric}_summary.txt`
- `command.txt`

### Standard compare (`--standard-compare`)
- `standard_comparison_{metric}.png`
- `standard_compare_{metric}.csv`
- `standard_compare_{metric}_summary.txt` - Human-readable summary (same as console output)

### Pose-variance methods
- `pose_variance_correlation_{test_type_base}.png`
- `pose_distribution_vs_distance_means.png`
- `source_pose_vs_distance_diff_{source_variant}.png`

## Notes

- Use `--list-variants` to discover and explore available variants before extraction
- For `-c`, use `-i` to specify where extracted CSV files are located (default: same as `-o`)
- For `--standard-compare` and `--sum`, use `-i` as the parent folder that contains variant directories
- `-i` is not needed for comparison/analysis operations that work directly from run data: `--pose-variance`, `--pose-dist-variance`, `--pose-diff-vs-source`
- For `--pose-variance`, pass at least one variant path for a test type base (the script discovers sibling variants by prefix).
- For `--pose-dist-variance`, provide at least 2 valid variant paths.
- For `--pose-diff-vs-source`, provide at least 3 paths total: 1 source + at least 2 comparison variants.
- `--pose-diff-method` defaults to `nearest`; use `centroid` for the legacy behavior.

