# Navigation Test Statistical Comparison Tool

Python tool to extract and compare navigation metrics from RoboVAST result folders.

The script supports:
- metric extraction (`-t`)
- statistical comparison (`-c`)
- summed-distribution comparison (`--sum`)
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

-c, --compare TEST1 TEST2
    Compare two extracted test types by name (name = folder basename used in CSV filenames)

--sum TEST1 TEST2 TARGET
    Sum distributions of TEST1+TEST2 and compare with TARGET

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

### 4) Compare two extracted test types (`-c`)

Note: `-c` uses test type names (folder basenames), not paths.

```bash
python3 compare_navigation_tests.py -c \
  mt-semantic-area-sampling-1-1 mt-semantic-area-sampling-1-2 \
  -m time distance \
  -o semantic_area_sampling_outputs
```

### 5) Compare localization-error metrics after extraction

```bash
python3 compare_navigation_tests.py -c \
  mt-remove-by-area-1-1-1 mt-remove-by-area-1-1-2 \
  -m loc_error_mean loc_error_var \
  -o remove_by_area_outputs
```

### 6) Sum distributions and compare to target (`--sum`)

```bash
python3 compare_navigation_tests.py --sum \
  mt-remove-by-area-1-1-2 mt-remove-by-area-1-1-3 mt-remove-by-area-1-1-1 \
  -m time distance \
  -o remove_by_area_outputs
```

### 7) Sum with each method (`--sum-method`)

#### pairwise (default)
```bash
python3 compare_navigation_tests.py --sum \
  mt-remove-by-area-1-1-2 mt-remove-by-area-1-1-3 mt-remove-by-area-1-1-1 \
  --sum-method pairwise \
  -m distance \
  -o remove_by_area_outputs
```

#### convolution
```bash
python3 compare_navigation_tests.py --sum \
  mt-remove-by-area-1-1-2 mt-remove-by-area-1-1-3 mt-remove-by-area-1-1-1 \
  --sum-method convolution \
  -m distance \
  -o remove_by_area_outputs
```

### 8) Pose variance analysis across variants (`--pose-variance`)

```bash
compare_navigation_tests.py --pose-variance \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-1 \
  -o semantic_area_sampling_outputs
```

### 9) New method: pose-position distribution vs distance-mean distribution (`--pose-dist-variance`)

```bash
python3 compare_navigation_tests.py --pose-dist-variance \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-1 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-2 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-3 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-4 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-5 \
  -o semantic_area_sampling_outputs
```

### 10) New method: source-vs-others pose/distance difference correlation (`--pose-diff-vs-source`)

```bash
python3 compare_navigation_tests.py --pose-diff-vs-source \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-1 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-2 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-3 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-4 \
  results/run-2026-02-28-030930/mt-semantic-area-sampling-1-5 \
  -o semantic_area_sampling_outputs
```

### 11) Quiet mode for long runs (`--no-display`)

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
- `sum_comparison_{test1}+{test2}_vs_{target}_{metric}.csv`
- `distribution_{test1}+{test2}_{metric}.png`
- `comparison_{test1}+{test2}_vs_{target}_{metric}.png`

### Pose-variance methods
- `pose_variance_correlation_{test_type_base}.png`
- `pose_distribution_vs_distance_means.png`
- `source_pose_vs_distance_diff_{source_variant}.png`

## Notes

- For `--pose-variance`, pass at least one variant path for a test type base (the script discovers sibling variants by prefix).
- For `--pose-dist-variance`, provide at least 2 valid variant paths.
- For `--pose-diff-vs-source`, provide at least 3 paths total: 1 source + at least 2 comparison variants.
- `-c` and `--sum` expect extracted CSVs to already exist in `-o` output directory.

