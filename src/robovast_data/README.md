# robovast-data

**A RoboVAST campaign's data in pandas and SQL, from its directory, its archive or a service.**

A campaign directory is the database. Its records are the bags, logs, `campaign.db` and the
files each run wrote. A table is built from those records the first time something names it,
into the campaign's own `.cache/`. It is built by the same decoder a RoboVAST service uses
([robovast-decode](https://pypi.org/project/robovast-decode/)), so a notebook and the web UI
read the same rows. No service, no ROS and no container image are needed.

```bash
pip install robovast-data
```

```python
from robovast_data import Campaign, Corpus, open_data, read_table, read_runs

c = Campaign("~/Downloads/nav-through-poses-2026-09-23-11155362")   # or the .tar.gz
c.runs                                   # one row per run: status, duration, param_* per factor
c.tables                                 # what can be built, and for how many runs it is
poses = c.table("poses", with_params=True)          # built on first use, then read
one = c.table("poses", config="<config>", run=0)    # builds that one run only
c.sql("SELECT config_name, avg(duration_s) FROM runs GROUP BY 1")
c.config("<config>").yaml("files/nav2_params.yaml")

Corpus("~/Downloads/nav-through-poses-*").table("action_navigate_through_poses_status")

poses = read_table("~/Downloads/nav-…", "poses", with_params=True)   # the one-liners
runs = read_runs("~/Downloads/nav-…")
```

`open_data(path)` returns what a path selects:

- a campaign directory selects the whole campaign;
- a configuration's directory selects that configuration;
- a run's directory, or anything inside it, selects that run.

`runs`, `table()` and `sql()` then answer for that node only. This is how the same notebook
cell works on a laptop and in RoboVAST's Results Explorer.

A campaign on a service opens by its URL, with the service's token (`vast service token`
prints it), and answers `runs`, `tables`, `table()` and `sql()` without a download:

```python
c = Campaign("https://<service>/campaigns/<campaign_id>", token="<token>")
c.table("poses", config="<config>", run=0)
```

The service builds what each call names and sends the rows as CSV, so pandas types the
columns. A query to a service takes no parameters, and `config()` needs the campaign on disk.

## Queries

`sql()` takes one `SELECT` and runs it with [DuckDB](https://duckdb.org/).

**What a query can name:**

- every table;
- `runs`;
- the campaign's record under the `campaign` schema (`campaign.run`, `campaign.unit`, ...);
- the views `run_view`, `config_view`, `container_failure_view`, `run_validity_view` and
  `pose_track_view`.

**What a query builds.** A query builds only the runs its `WHERE` clause restricts a table to,
when it does so with `config_name = ...` or `run_id IN (...)`. Otherwise it builds every run in
scope.

**What a query may touch.** It can read the campaign's tables and nothing else on disk.

**Spellings kept from older engines.** `CAST(x AS REAL)` means a double. `CAST(x AS INTEGER)`
truncates. `PERCENTILE(value, p)` takes `p` from 0 to 100. `REGEXP(pattern, value)` searches.

Part of [RoboVAST](https://cps-test-lab.github.io/robovast/).
