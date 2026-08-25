#!/usr/bin/env python3
"""Retrigger a campaign that just ran, and prove the relaunch actually executes.

Run from CI after ``test_vast.py``, against the results directory it produced. That is the one
place a *real* retrigger is cheap: the campaign's records exist, and the image it recorded is
present because this run built it.

Unit tests cover the pre-flight and the staging against committed historic fixtures
(``tests/service/test_historic_campaigns.py``), and neither can prove the thing this path
actually claims -- that a campaign relaunched from its own records runs. This can, and it costs
one extra campaign of the smallest example in the repo.

It also puts the container-protocol window in front of a freshly built image, so a label that
disagrees with the host is caught here instead of by whoever tries to re-run a campaign a year
from now.
"""

import argparse
import sys
from pathlib import Path


def _campaign_dirs(results_dir: Path) -> list:
    """Campaign directories under *results_dir*, newest last."""
    from robovast.common.execution import is_campaign_dir

    return sorted(d for d in results_dir.iterdir() if d.is_dir() and is_campaign_dir(d.name))


def _report_axes(report: dict) -> None:
    for name, axis in sorted(report["axes"].items()):
        print(f"  {name:<10} {axis['verdict']:<12} {axis['detail']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_dir", type=Path,
                        help="The results directory test_vast.py wrote into.")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from robovast.service import retrigger

    campaigns = _campaign_dirs(args.results_dir)
    if not campaigns:
        print(f"ERROR: no campaign directory under {args.results_dir}", file=sys.stderr)
        return 1
    source = campaigns[-1]
    print(f"source campaign: {source.name}")

    report = retrigger.check(source, source.name)
    _report_axes(report)
    if not report["runnable"]:
        print(f"ERROR: a campaign that just ran successfully reports as not re-runnable: "
              f"{', '.join(report['blocking'])}", file=sys.stderr)
        return 1

    # The image was built and run in this job, so the protocol axis must be a definite OK --
    # not `unknown`. `unknown` here would mean the freshly built image reports no version at
    # all, which is exactly the drift the label was added to make visible.
    host = report["axes"]["host"]
    if host["verdict"] != "ok":
        print(f"ERROR: the image built in this run reports protocol '{host['verdict']}': "
              f"{host['detail']}", file=sys.stderr)
        return 1

    # Staging is the last step before a launch and the one that touches every record, so a
    # failure here is reported separately from a failed run: they have different causes.
    from robovast.service.interface import CreateCampaignRequest
    staging_root = args.results_dir / "_retrigger_staging"
    plan = retrigger.prepare(source, source.name, workspaces_root=staging_root,
                             description_limit=200, request_model=CreateCampaignRequest)
    try:
        print(f"staged {plan.config_path}")
        print(f"pinned images: {plan.pinned_images}")
        if plan.config_migration:
            print(f"config migrated: {plan.config_migration}")
        plan.materialize()
        missing = retrigger.missing_run_files(source, plan.staging_dir)
        if missing:
            print(f"ERROR: staged tree is missing run files: {missing}", file=sys.stderr)
            return 1
    finally:
        plan.discard()

    print("retrigger pre-flight and staging OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
