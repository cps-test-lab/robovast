# robovast-decode

**A RoboVAST campaign's tables, built from its recordings, anywhere Python runs.**

A campaign records what its runs did: ROS 2 bags (mcap), the containers' logs, the simulator's
own recording. This package turns those files into tables -- `poses`, `nav2_behavior_tree`,
`costmaps`, `action_<name>_feedback`, a table per recorded topic -- as parquet files beside the
recordings, under the campaign directory's `.cache/`. It needs no ROS installation and no
image of the system under test: message definitions come from the recording itself, from the
definitions file a run writes beside its bag, and from the ROS 2 distro's standard types.

```bash
pip install robovast-decode
robovast-decode tables  path/to/campaign            # what the recordings can give, and what is built
robovast-decode build   path/to/campaign --table poses
```

A run's own `*.csv` and `*.jsonl` files are tables too, named after the file and typed from
their values, and `runs` -- one row per run with its outcome, host and every varied factor as a
typed `param_*` column -- is read from the campaign's `campaign.db` (`robovast_decode.runs`).

A table is built for a run once and kept; building again rebuilds only what changed. The
tables are the same ones a RoboVAST service builds, row for row: a pose is resolved with
`tf2`'s own rules (its caches, its extrapolation refusals, its interpolation), not with a
look-alike.

Part of [RoboVAST](https://cps-test-lab.github.io/robovast/).
