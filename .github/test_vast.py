#!/usr/bin/env python3
"""Generic test script for VAST files - tests execution and postprocessing."""

import argparse
import math
import os
import re
import signal
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path


def run_command(cmd, repo_root, cwd=None, check=True, stream_output=False):
    """Run a command, stream its output, and return the exit code.

    On failure always prints clearly labelled stdout and stderr so CI logs
    contain enough context to debug the problem.
    """
    print(f"Running: {cmd}")

    if stream_output:
        # Stream output directly to stdout for live visibility
        result = subprocess.run(
            ['poetry', 'run', '--directory', str(repo_root),
            'bash', '-c', f'cd {cwd} && {cmd}'],
            text=True,
            check=False,
        )
    else:
        result = subprocess.run(
            ['poetry', 'run', '--directory', str(repo_root),
            'bash', '-c', f'cd {cwd} && {cmd}'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        # Always emit stdout so progress is visible in the CI log.
        if result.stdout:
            sys.stdout.write(result.stdout)
            sys.stdout.flush()

    if result.returncode != 0:
        print(f"\n✗ Command exited with code {result.returncode}")
        if hasattr(result, 'stdout') and result.stdout:
            print("--- stdout ---")
            print(result.stdout)
        if hasattr(result, 'stderr') and result.stderr:
            print("--- stderr ---")
            print(result.stderr)
        if check:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, getattr(result, 'stdout', ''), getattr(result, 'stderr', '')
            )

    return result.returncode


def check_campaign_dir_structure(campaign_dir):  # pylint: disable=too-many-return-statements
    """Check that one campaign's directory has the expected structure.

    The campaign's own directory rather than a results root to search: on a cluster every
    workflow of a run lands under the service's one results volume, so "the first campaign
    directory found" would be whichever ran first, not the one just launched.
    """
    first_run = Path(campaign_dir)

    if not first_run.is_dir():
        print(f"✗ Campaign directory does not exist: {campaign_dir}")
        return False

    print(f"  Checking structure of {first_run.name}:")
    
    # Look for expected files/directories in campaign directory
    campaign_contents = list(first_run.iterdir())
    print(f"    Contents: {[c.name for c in campaign_contents]}")

    # Check for scenario.osc file in _config/
    config_dir_check = first_run / '_config'
    if config_dir_check.exists():
        scenario_osc = config_dir_check / 'scenario.osc'
        if not scenario_osc.exists():
            print("  ✗ scenario.osc file not found in _config/ directory")
            return False
        print("  ✓ scenario.osc file exists in _config/")
    else:
        print("  ✗ _config directory not found in campaign directory")
        return False
    
    # Check for execution.yaml file in _execution/
    execution_dir = first_run / '_execution'
    if not execution_dir.exists() or not (execution_dir / 'execution.yaml').exists():
        print("  ✗ execution.yaml file not found in _execution/ directory")
        return False

    print("  ✓ execution.yaml file exists in _execution/")

    # What postprocessing leaves behind: the example's postprocessing step writes this
    # marker into the campaign root, and the cluster lane's postprocessing pod delivers it
    # back to the results volume with everything else it changed. Its absence means the
    # step never ran, whatever phase the campaign reports.
    if not (first_run / '.postprocessed').exists():
        print("  ✗ .postprocessed not found in the campaign root: postprocessing did not run")
        return False
    print("  ✓ .postprocessed exists: postprocessing ran")

    # Check for config directories
    config_dirs = [d for d in campaign_contents if d.is_dir()]
    if not config_dirs:
        print("  ✗ No config directories found in campaign")
        return False
    
    print(f"  ✓ Found {len(config_dirs)} config directory/directories")
    
    # Check for _config directory
    config_dir = [d for d in config_dirs if d.is_dir() and d.name == '_config']
    if not config_dir:
        print("  ✗ _config directory not found in campaign")
        return False
    
    print("  ✓ _config directory exists")
    
    # Check structure of first scenario directory (exclude _config; it has its own
    # layout, and exclude the job-level artifact tree _jobs/).
    scenario_dirs = [
        d for d in config_dirs
        if d.name not in ('_config', '_transient', '_execution', '_jobs')
    ]
    if not scenario_dirs:
        print("  ✗ No scenario directory found in run")
        return False
    first_scenario = scenario_dirs[0]
    config_contents = list(first_scenario.iterdir())
    

    transient_dir = [d for d in config_dirs if d.name == '_transient']
    if not transient_dir:
        print("  ✗ _transient directory not found in run")
        return False
    
    print("  ✓ _transient directory exists")
    
    # Check for configurations.yaml in _transient directory
    transient_dir_path = transient_dir[0]
    transient_dir_contents = list(transient_dir_path.iterdir())
    configurations_yaml = [f for f in transient_dir_contents if f.name == 'configurations.yaml']
    if not configurations_yaml:
        print("  ✗ configurations.yaml file not found in _transient directory")
        return False
    
    print("  ✓ configurations.yaml file exists in _transient directory")

    run_dirs = [d for d in config_contents if d.name not in ('_config')]
    print(f"    {first_scenario.name} contents: {[c.name for c in run_dirs]}")
    
    # Check that only numeric directories exist (and require test.xml in each)
    for item in run_dirs:
        if item.is_dir():
            name = item.name
            if not name.isdigit():
                print(f"    ✗ Invalid directory name: {name} (expected numeric run index)")
                return False
            
            if name.isdigit():
                # Check for test.xml file in numeric directories
                numeric_dir_contents = list(item.iterdir())
                test_xml = [f for f in numeric_dir_contents if f.name == 'test.xml']
                if not test_xml:
                    print(f"    ✗ test.xml file not found in {name} directory")
                    return False

                # Every run links to its job's artifact directory via a `job`
                # symlink pointing into the campaign-level _jobs/ tree.
                job_link = item / 'job'
                if not job_link.is_symlink():
                    print(f"    ✗ 'job' symlink not found in {name} directory")
                    return False
                target = os.readlink(job_link)
                if '_jobs' not in target:
                    print(
                        f"    ✗ 'job' symlink in {name} does not point into _jobs/ "
                        f"(target: {target})"
                    )
                    return False

    print("    ✓ Directory names are valid (numeric run indices)")
    print("    ✓ test.xml files exist in numeric directories")
    print("    ✓ each run has a 'job' symlink into _jobs/")

    # Job-level artifact directories live under <campaign>/_jobs/job-N/.
    if not check_job_directories(first_run):
        return False

    return True


def check_job_directories(campaign_dir):
    """Check the campaign's job-level artifact directories (``_jobs/[batch-N/]job-N/``).

    Regardless of ``runs_per_job`` every run is dispatched through a job, so
    the campaign always has a ``_jobs/`` directory with one ``job-N`` subdir per
    job (namespaced under a ``batch-N/`` prefix on the batch campaign path),
    each holding that job's job-level artifacts (at minimum ``sysinfo.yaml``).
    """
    jobs_dir = campaign_dir / '_jobs'
    if not jobs_dir.is_dir():
        print("  ✗ _jobs directory not found in campaign")
        return False

    job_dirs = _job_dirs(jobs_dir)
    if not job_dirs:
        print("  ✗ No job-* directories found in _jobs/")
        return False

    print(f"  ✓ Found {len(job_dirs)} job director(y/ies) in _jobs/")

    for job_dir in job_dirs:
        if not (job_dir / 'sysinfo.yaml').exists():
            print(f"    ✗ sysinfo.yaml not found in _jobs/{job_dir.name}/")
            return False

    print("    ✓ sysinfo.yaml exists in each job directory")
    return True


def _scrub(text, secret=None):
    """*text* with an access token blanked: a known *secret* by value, and anything that
    follows ``token=`` or ``--token`` by shape.

    A deployment mints its token and prints a login URL carrying it, a login prints it
    back in the curl line it suggests, and the service log may repeat it. A CI cluster dies
    with the runner, so a leak is short-lived, but a CI log is kept longer than that.
    """
    if secret:
        text = text.replace(secret, "<scrubbed>")
    return re.sub(r"(token[=\s]+)\S+", r"\1<scrubbed>", text)


def capture_command(cmd, repo_root, cwd=None, secret=None, quiet=False):
    """Run a command and return ``(exit_code, stdout)``, without raising.

    *secret* is a value that must not reach the log: the command line is echoed with it
    blanked, and the output stays with the caller, since a login prints the token back in
    the curl line it suggests. *quiet* keeps the output with the caller for the same
    reason when the output itself is the secret, as reading the token back is.
    """
    print(f"Running: {_scrub(cmd, secret)}")
    result = subprocess.run(
        ['poetry', 'run', '--directory', str(repo_root),
         'bash', '-c', f'cd {cwd} && {cmd}'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
    )
    if result.stdout and secret is None and not quiet:
        sys.stdout.write(result.stdout)
        sys.stdout.flush()
    return result.returncode, result.stdout or ""


def refuse_a_foreign_service():
    """Fail if the port is already served, rather than testing whatever answers.

    A service that is not this one has its own results directory, so the campaign
    this test launches would land somewhere the test never looks -- a failure that
    names a missing directory and not the service it actually used.
    """
    from robovast.service.interface import DEFAULT_PORT

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(2)
        if probe.connect_ex(('127.0.0.1', DEFAULT_PORT)) == 0:
            raise RuntimeError(
                f"something is already listening on 127.0.0.1:{DEFAULT_PORT}; "
                "stop it before running this test, which forwards the service it "
                "deployed to that port and reads that deployment's results")


class ClusterSession:
    """A ``robovast-service`` deployed into a Kubernetes cluster, for the length of the script.

    The cluster lane runs only inside a cluster -- a campaign's pods deliver their outputs
    to the service over the cluster network -- so the service is deployed *into* the
    cluster the caller names (``--context``) with ``vast cluster setup``, and reached from
    here through a ``kubectl port-forward`` on the conventional port, which every client
    finds. One deployment serves every workflow of a run: unlike a local service, which
    each workflow started afresh with its own results directory, every campaign lands
    under the service's one results volume, so a workflow is told its campaign's directory
    rather than left to find "the" campaign under a root.

    That volume is a directory on the node (``--data-root``). The caller mounts a host
    directory there when creating the cluster -- kind's ``extraMounts``, minikube's
    ``--mount`` -- and names the host side as ``--data-mount``, which is where this script
    reads results from. The files arrive owned by the pods' user, so a step that must
    *write* under them chowns first.

    Images: ``--image`` sets ``ROBOVAST_PROJECT`` and ``ROBOVAST_PROJECT_TAG`` before setup
    runs, so the service pod, the sidecars and the campaign containers all come from the
    same family at the same tag. The cluster's kubelet has no registry login of its own,
    so a private registry needs ``ROBOVAST_REGISTRY_SERVER`` / ``_USERNAME`` / ``_PASSWORD``
    in the environment; setup turns them into the pull Secret every pod uses.
    """

    def __init__(self, repo_root, cwd, context, namespace, data_root, data_mount,
                 cluster_config):
        self.repo_root = repo_root
        self.cwd = cwd
        self.context = context
        self.namespace = namespace
        self.data_root = data_root
        self.cluster_config = cluster_config
        self.results_dir = os.path.join(data_mount, "results")
        self.pf = None
        self.pf_log = None
        self.ready = False
        self.token = None

    # A workflow enters the session; the session outlives it.
    def __enter__(self):
        self.ensure_ready()
        return self

    def __exit__(self, *_exc):
        return False

    def ensure_ready(self):
        if self.ready:
            return
        os.environ['ROBOVAST_CONFIG'] = os.path.join(self.cwd, 'robovast-login.json')
        refuse_a_foreign_service()
        self._setup()
        self._wait_for_rollout()
        self._port_forward()
        self._login()
        self.ready = True

    def _kubectl(self, *args, timeout=300):
        return subprocess.run(['kubectl', '--context', self.context, '-n', self.namespace,
                               *args], capture_output=True, text=True, check=False,
                              timeout=timeout)

    def _setup(self):
        """Deploy (or, with ``--force``, re-deploy) the service into the cluster.

        ``--force`` so the script can be run twice against the same cluster; a fresh CI
        cluster has nothing to force. ``--no-performance-governor``: that step wants to set
        the nodes' CPU governor, which a container node has none of.
        """
        cmd = (f"vast cluster setup {self.cluster_config} --context {self.context} "
               f"-n {self.namespace} --data-root {self.data_root} "
               f"--no-performance-governor --force")
        print(f"Deploying: {cmd}")
        code, out = capture_command(cmd, self.repo_root, cwd=self.cwd)
        with open(os.path.join(self.cwd, 'cluster-setup.log'), 'w', encoding='utf-8') as fh:
            fh.write(_scrub(out))
        if code != 0:
            raise RuntimeError(f"vast cluster setup exited {code}; its output follows:\n"
                               f"{_scrub(out)[-4000:]}")
        print("✓ robovast-service deployed")

    def _wait_for_rollout(self, timeout=300):
        res = self._kubectl('rollout', 'status', 'deployment/robovast-service',
                            f'--timeout={timeout}s', timeout=timeout + 30)
        if res.returncode != 0:
            raise RuntimeError(f"the service Deployment did not roll out:\n{res.stdout}"
                               f"{res.stderr}\n{self._diagnostics()}")
        print("✓ robovast-service rolled out")

    def _token(self):
        code, out = capture_command(
            f"vast service token -q -x {self.context} -n {self.namespace}",
            self.repo_root, cwd=self.cwd, quiet=True)
        if code != 0 or not out.strip():
            raise RuntimeError("could not read the deployed access token back")
        self.token = out.strip().splitlines()[-1]
        return self.token

    def _port_forward(self):
        from robovast.service.interface import DEFAULT_PORT

        self.pf_log = open(os.path.join(self.cwd, 'port-forward.log'), 'w', encoding='utf-8')
        self.pf = subprocess.Popen(
            ['kubectl', '--context', self.context, '-n', self.namespace, 'port-forward',
             'svc/robovast-service', f'{DEFAULT_PORT}:{DEFAULT_PORT}'],
            stdout=self.pf_log, stderr=subprocess.STDOUT, text=True, start_new_session=True)

    def _login(self, timeout=120):
        from robovast.service.interface import DEFAULT_PORT

        url = f"http://127.0.0.1:{DEFAULT_PORT}"
        token = self._token()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.pf.poll() is not None:
                raise RuntimeError(f"the port-forward exited with {self.pf.returncode}")
            code, _ = capture_command(
                f"vast login {url} --token {token} --name ci --no-link",
                self.repo_root, cwd=self.cwd, secret=token)
            if code == 0:
                print("✓ robovast-service is answering through the port-forward, and this "
                      "client is logged in")
                capture_command('vast doctor', self.repo_root, cwd=self.cwd)
                return
            time.sleep(3)
        raise RuntimeError(f"the service did not answer within {timeout}s\n"
                           f"{self._diagnostics()}")

    def _diagnostics(self):
        pods = self._kubectl('get', 'pods', '-o', 'wide', timeout=60)
        events = self._kubectl('get', 'events', '--sort-by=.lastTimestamp', timeout=60)
        return (f"--- pods ---\n{pods.stdout}{pods.stderr}\n"
                f"--- events (tail) ---\n{events.stdout[-3000:]}{events.stderr}")

    def close(self):
        """Stop the port-forward and surface the service log, whatever happened."""
        if self.pf is not None and self.pf.poll() is None:
            try:
                os.killpg(os.getpgid(self.pf.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.pf.send_signal(signal.SIGTERM)
            try:
                self.pf.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.pf.kill()
        if self.pf_log is not None:
            self.pf_log.close()
        if self.ready or self.pf is not None:
            log = self._kubectl('logs', 'deployment/robovast-service', '--all-containers',
                                '--tail=150', timeout=60)
            print("--- robovast-service log (tail) ---")
            print(_scrub(log.stdout + log.stderr, self.token))


#: The cluster session of this run, deployed into the context ``--context`` named.
_CLUSTER = None


def service_context(repo_root, results_dir, cwd):
    """The service a workflow runs through: the run's cluster deployment."""
    del repo_root, results_dir, cwd
    if _CLUSTER is None:
        raise RuntimeError("no service: this script runs against a cluster (--context)")
    return _CLUSTER


def test_vast_workflow(vast_file_path, test_directory, config=None, runs=None):  # pylint: disable=too-many-return-statements
    """Test the complete workflow: serve -> workspace init -> workspace run -> postprocess.

    This is the only end-to-end test of the whole stack, so it drives the real path a user
    takes: a campaign runs a *workspace's* project through a service. It used to call
    ``vast exec local run``, an in-process Docker lane with no service and no workspace,
    which no longer exists -- and which tested a path the documentation did not describe.

    Returns the campaign's directory on success, ``None`` on failure: on a cluster every
    workflow's campaign lands under one results root, so the caller comparing two of them
    needs to be told which is which.
    """
    print("\n" + "="*60)
    print("Testing: Complete VAST workflow")
    print("="*60)
    print(f"Test directory: {test_directory}")
    print(f"VAST file: {vast_file_path}")

    repo_root = Path(__file__).parent.parent
    config_path = Path(vast_file_path)
    results_dir = os.path.join(test_directory, "results")

    if not config_path.is_absolute():
        config_path = repo_root / config_path

    if not config_path.exists():
        print(f"✗ Config file not found: {config_path}")
        return None

    print(f"✓ Config file found: {config_path}")

    project_dir = config_path.parent
    workspace_name = f"citest-{project_dir.name}"

    try:
        with service_context(repo_root, results_dir, test_directory) as service:
            # Step 1: push the workspace. A service cannot read the caller's disk, so
            # this is the one step that has to happen client-side.
            #
            # Every later step addresses the workspace by the id `init` reports, never by
            # the name asked for: the service auto-suffixes a name that is taken
            # (`foo` -> `foo-2`), so on the second workflow of a run the requested name
            # still resolves -- to the *previous* workflow's workspace. The packing test
            # runs two workflows over the same name, and its packed half silently
            # re-ran the baseline's project that way: same job count, and a comparison
            # that looked like a packing regression rather than a stale workspace.
            print("\n--- Step 1: vast workspace init ---")
            code, out = capture_command(
                f"vast workspace init {project_dir} --name {workspace_name}",
                repo_root, cwd=test_directory)
            if code != 0:
                print("✗ vast workspace init failed")
                return None
            workspace_id = _workspace_id_from_init(out)
            if not workspace_id:
                print(f"✗ could not read the workspace id out of:\n{out}")
                return None
            print(f"✓ workspace pushed as {workspace_id}")

            # Step 2: validate before spending any compute. Reports every problem at
            # once, and costs nothing.
            print("\n--- Step 2: vast workspace validate ---")
            code = run_command(
                f"vast workspace validate {workspace_id} {config_path.name}",
                repo_root, cwd=test_directory, check=False)
            if code != 0:
                print("✗ vast workspace validate failed")
                return None
            print("✓ project validates")

            # Step 3: launch, then wait for it as its own command. `vast campaign wait`
            # exits only once the campaign is genuinely over, and its exit code is the
            # answer.
            print("\n--- Step 3: vast workspace run ---")
            cmd_run = f"vast workspace run {workspace_id} {config_path.name}"
            if runs:
                cmd_run += f" -r {runs}"
            if config:
                cmd_run += f" --filter {config}"
            cmd_run += ' --description "CI: end-to-end workflow"'
            code, out = capture_command(cmd_run, repo_root, cwd=test_directory)
            if code != 0:
                print("✗ vast workspace run failed")
                return None
            campaign_id = _campaign_id_from_launch(out)
            if not campaign_id:
                print(f"✗ could not read the campaign id out of:\n{out}")
                return None
            print(f"✓ launched {campaign_id}")

            print("\n--- Step 4: vast campaign wait ---")
            # Run as the whole command, unwrapped: anything appended would report the
            # wrapper's status and turn a failed campaign into a reported success.
            code = run_command(f"vast campaign wait {campaign_id}", repo_root,
                               cwd=test_directory, check=False, stream_output=True)
            if code != 0:
                print(f"✗ vast campaign wait exited {code} "
                      "(1 failed/stopped, 2 timeout, 3 no phase, 4 stalled, "
                      "5 health finding)")
                return None
            verdict = _postprocessing_failure(campaign_id, repo_root, test_directory)
            if verdict:
                print(f"✗ the campaign finished, but {verdict}")
                return None
            print("✓ campaign finished, postprocessing included")

            # Step 5: re-run postprocessing through the service. Inside the service
            # block, because postprocessing acts on a campaign, on whichever lane its runs
            # executed. Running it as `vast results postprocess` against the results tree
            # instead would force this step after the shutdown.
            #
            # Dispatched, not awaited, exactly like a launch: the campaign re-enters its
            # postprocessing phase, so the wait is what says it worked.
            print("\n--- Step 5: vast campaign postprocess ---")
            code = run_command(f"vast campaign postprocess {campaign_id}",
                               repo_root, cwd=test_directory)
            if code != 0:
                print("✗ vast campaign postprocess failed")
                return None
            code = run_command(f"vast campaign wait {campaign_id}", repo_root,
                               cwd=test_directory, check=False, stream_output=True)
            if code != 0:
                print(f"✗ wait after postprocess exited {code}")
                return None
            verdict = _postprocessing_failure(campaign_id, repo_root, test_directory)
            if verdict:
                print(f"✗ {verdict}")
                return None
            print("✓ postprocessing re-ran through the service")

        # The results tree is read directly from here: a local service is down, and a
        # cluster's results volume is mounted from the host.
        campaign_dir = Path(service.results_dir) / campaign_id
        if not check_campaign_dir_structure(campaign_dir):
            return None
        print("✓ Output structure is valid")

        print("\n✓ Complete workflow succeeded!")
        return campaign_dir

    except subprocess.CalledProcessError as e:
        print(f"✗ Command failed with exit code {e.returncode}")
        return None
    except Exception as e:
        print(f"✗ Unexpected error: {e}")
        traceback.print_exc()
        return None


def _postprocessing_failure(campaign_id, repo_root, cwd):
    """What the service says went wrong with *campaign_id*'s postprocessing, or ``""``.

    ``vast campaign wait`` exits 0 for a campaign whose runs finished and whose
    postprocessing then failed: the campaign *did* finish, and the failure goes to stderr
    as one ``postprocessing failed:`` line. The exit code the steps branch on therefore
    says nothing about the one step this script exists to exercise, so the campaign is
    asked once more here -- it is over, so the wait returns at once -- and that line is
    the verdict.
    """
    _code, out = capture_command(f"vast campaign wait {campaign_id}", repo_root, cwd=cwd,
                                 quiet=True)
    for line in out.splitlines():
        if "postprocessing failed" in line:
            return line.strip()
    return ""


def _workspace_id_from_init(output):
    """The workspace id from ``workspace init``'s confirmation line.

    It prints ``workspace <id> (<name>) initialized from <dir> (<n> files)``. The id and
    not the name, because the name it reports may not be the name that was asked for.
    """
    match = re.search(r"^workspace (\S+) \(", output, re.MULTILINE)
    return match.group(1) if match else ""


def _campaign_id_from_launch(output):
    """The campaign id from ``workspace run``'s confirmation line.

    It prints ``Launched campaign '<id>'.``; the id is not otherwise knowable, because the
    *service* names the campaign -- CreateCampaignRequest carries no id, so the caller
    cannot choose one up front.
    """
    match = re.search(r"Launched campaign '([^']+)'", output)
    return match.group(1) if match else ""


def _job_dirs(jobs_dir):
    """Return the ``job-N`` artifact directories under ``_jobs/``.

    Jobs may sit directly under ``_jobs/`` or be namespaced under a
    ``batch-N/`` prefix (as the batch campaign path does), so match at any
    depth.
    """
    return [d for d in jobs_dir.rglob('job-*') if d.is_dir()]


def _count_job_dirs(campaign_dir):
    """Count ``_jobs/[batch-N/]job-N`` directories in a campaign."""
    jobs_dir = campaign_dir / '_jobs'
    if not jobs_dir.is_dir():
        return 0
    return len(_job_dirs(jobs_dir))


#: A job's parameter file, with or without a batch prefix: ``job-3.params.yaml``,
#: ``batch-0-job-3.params.yaml``. Per job by construction, so per packing.
_JOB_PARAMS = re.compile(r"(?:.+-)?job-\d+\.params\.yaml")


def _collect_non_job_files(campaign_dir):
    """Collect campaign-relative file paths, excluding all job-specific artifacts.

    The job *packing* (``runs_per_job``) only changes how runs are grouped
    into jobs; the per-config/per-run scenario output must be identical. This
    returns the set of files that should match regardless of packing by skipping:

    - the ``_jobs/`` artifact tree,
    - the per-run ``job`` symlinks,
    - the ``_transient/`` job bookkeeping: ``job_links.yaml`` and each job's
      ``params.yaml``, which the cluster lane namespaces by batch
      (``batch-0-job-N.params.yaml``) and so is matched by shape, not prefix.
    """
    result = set()
    for root, dirs, files in os.walk(campaign_dir, followlinks=False):
        # Prune the job artifact tree and any symlinked dirs (the `job` links).
        dirs[:] = [
            d for d in dirs
            if d != '_jobs' and not os.path.islink(os.path.join(root, d))
        ]
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), campaign_dir)
            parts = rel.split(os.sep)
            # Skip per-job transient bookkeeping: job_links.yaml, and a job's params
            # file under whichever batch prefix the lane gives it.
            if parts[0] == '_transient' and (
                    fn == 'job_links.yaml' or _JOB_PARAMS.fullmatch(fn)):
                continue
            result.add(rel)
    return result


def _set_runs_per_job(text, value):
    """Return *text* with ``runs_per_job: <value>`` set in the execution block."""
    out = []
    in_execution = False
    inserted = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip('\n')
        if stripped == 'execution:':
            out.append(line)
            out.append(f"  runs_per_job: {value}\n")
            in_execution = True
            inserted = True
            continue
        # Drop any pre-existing runs_per_job entry so ours is authoritative.
        if in_execution and stripped.startswith('  runs_per_job:'):
            continue
        # A new top-level (unindented) key ends the execution block.
        if in_execution and line[:1].strip() and stripped.endswith(':'):
            in_execution = False
        out.append(line)
    if not inserted:
        raise ValueError("Could not find an 'execution:' block in the VAST file")
    return ''.join(out)


def test_runs_per_job_packing(vast_file_path, test_directory, config=None, runs=None):  # pylint: disable=too-many-return-statements
    """Verify runs_per_job>1 packs runs into fewer jobs but keeps output identical.

    Runs the same campaign twice — once with the default packing
    (``runs_per_job=1``, one job per run) and once with
    ``runs_per_job: 10`` temporarily injected into the VAST file — then
    asserts that:

    - the packed run produces fewer jobs (``ceil(N/10)`` instead of ``N``), and
    - every non-job output file is byte-for-byte present in both layouts.
    """
    print("\n" + "=" * 60)
    print("Testing: runs_per_job packing equivalence")
    print("=" * 60)

    repo_root = Path(__file__).parent.parent
    config_path = Path(vast_file_path)
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    if not config_path.exists():
        print(f"✗ Config file not found: {config_path}")
        return False

    # Packing only differs when a job holds more than one work item, so force at
    # least two runs (each run of the selected config is one work item).
    run_count = runs if (runs and runs >= 2) else 2

    base_dir = os.path.join(test_directory, "cpj_base")
    packed_dir = os.path.join(test_directory, "cpj_packed")
    os.makedirs(base_dir, exist_ok=True)
    os.makedirs(packed_dir, exist_ok=True)

    # 1. Baseline: default packing (runs_per_job=1 → one job per run).
    print("\n--- Baseline run (runs_per_job=1) ---")
    base_campaign = test_vast_workflow(vast_file_path, base_dir, config, run_count)
    if base_campaign is None:
        print("✗ Baseline (runs_per_job=1) workflow failed")
        return False

    # 2. Packed: temporarily inject runs_per_job: 10 and re-run.
    print("\n--- Packed run (runs_per_job=10) ---")
    original_text = config_path.read_text(encoding="utf-8")
    try:
        config_path.write_text(
            _set_runs_per_job(original_text, 10), encoding="utf-8"
        )
        packed_campaign = test_vast_workflow(vast_file_path, packed_dir, config, run_count)
        if packed_campaign is None:
            print("✗ Packed (runs_per_job=10) workflow failed")
            return False
    finally:
        # Always restore the original VAST file, even on failure.
        config_path.write_text(original_text, encoding="utf-8")

    # 3. Compare the two campaign outputs.
    print("\n--- Comparing outputs ---")

    base_jobs = _count_job_dirs(base_campaign)
    packed_jobs = _count_job_dirs(packed_campaign)
    expected_packed = math.ceil(base_jobs / 10) if base_jobs else 0
    print(
        f"  Baseline jobs: {base_jobs}, packed jobs: {packed_jobs} "
        f"(expected {expected_packed})"
    )
    if base_jobs <= packed_jobs:
        print("  ✗ runs_per_job=10 did not reduce the number of jobs")
        return False
    if packed_jobs != expected_packed:
        print(f"  ✗ Unexpected packed job count: {packed_jobs} != {expected_packed}")
        return False
    print("  ✓ Job packing reduced the job count as expected")

    base_files = _collect_non_job_files(base_campaign)
    packed_files = _collect_non_job_files(packed_campaign)
    only_base = base_files - packed_files
    only_packed = packed_files - base_files
    if only_base or only_packed:
        print("  ✗ Non-job output differs between runs_per_job=1 and =10")
        if only_base:
            print(f"    Only in baseline: {sorted(only_base)}")
        if only_packed:
            print(f"    Only in packed:   {sorted(only_packed)}")
        return False

    print(f"  ✓ Non-job output identical ({len(base_files)} files) across packings")
    print("\n✓ runs_per_job packing test succeeded!")
    return True


def main():
    """Run all tests."""
    parser = argparse.ArgumentParser(
        description='Test VAST file workflow: init -> execution -> postprocessing'
    )
    parser.add_argument(
        'vast_file',
        type=str,
        help='Path to the VAST configuration file (absolute or relative to repo root)'
    )
    parser.add_argument(
        '-c', '--config',
        type=str,
        default=None,
        help='Configuration to run (passed as --filter <config> to vast workspace run)'
    )
    parser.add_argument(
        '-d', '--test-directory',
        type=str,
        required=True,
        help='Directory the test runs in and writes its output to'
    )
    parser.add_argument(
        '-r', '--runs',
        type=int,
        default=None,
        help='Number of runs (passed as -r <runs> to vast workspace run)'
    )
    parser.add_argument(
        '--context', '-x',
        type=str,
        default=None,
        required=True,
        help='Run through a robovast-service deployed into this kubeconfig context '
             '(vast cluster setup). Needs --data-root and --data-mount.'
    )
    parser.add_argument(
        '--namespace', '-n',
        type=str,
        default='default',
        help='Namespace the service is deployed into (with --context)'
    )
    parser.add_argument(
        '--data-root',
        type=str,
        default=None,
        help='Node path the deployment keeps its data under (with --context); the '
             'results volume is <data-root>/results on the node'
    )
    parser.add_argument(
        '--data-mount',
        type=str,
        default=None,
        help='Host path mounted at --data-root on the node, where this script reads the '
             'results from (with --context)'
    )
    parser.add_argument(
        '--cluster-config',
        type=str,
        default='minikube',
        help='The `vast cluster setup` configuration: minikube is the one-node hostPath '
             'deployment, which is what a kind node is too'
    )
    parser.add_argument(
        '--no-packing-test',
        action='store_true',
        help='Skip the runs_per_job packing-equivalence test (it runs the '
             'campaign twice and is more expensive)'
    )
    parser.add_argument(
        '--image',
        type=str,
        default=None,
        help='Run against this image instead of the published family, given as '
             '<project>/<member>:<tag> (e.g. a PR-built image). Set as '
             'ROBOVAST_PROJECT + ROBOVAST_PROJECT_TAG rather than written into the '
             '.vast: a .vast names only its own images, and the framework ones are '
             'symbolic family: refs until core resolves them.'
    )

    args = parser.parse_args()

    repo_root = Path(__file__).parent.parent
    config_path = Path(args.vast_file)
    if not config_path.is_absolute():
        config_path = repo_root / config_path

    print("="*60)
    print("VAST Workflow Tests")
    print("="*60)
    print(f"VAST file: {args.vast_file}")
    if args.config:
        print(f"Configuration: {args.config}")
    if args.test_directory:
        print(f"Test directory: {args.test_directory}")
    if args.image:
        print(f"Image override: {args.image}")

    if args.image:
        # `execution.image` was v1's shape. In v2 a .vast names its OWN images and nothing
        # else; the framework's are `family:<member>` refs resolved from ROBOVAST_PROJECT
        # (where) and ROBOVAST_PROJECT_TAG (which) -- see robovast.common.execution. So the
        # override moves the whole family at once instead of pinning one container, and the
        # run still exercises family resolution rather than bypassing it.
        # A digest reference parses without complaint and produces nonsense: rpartition(':')
        # on "reg/org/robovast@sha256:<hex>" yields project "reg/org/robovast@sha256" and tag
        # "<hex>", which recompose into "reg/org/robovast:<hex>" -- an image that does not
        # exist, reported eight minutes later as "manifest unknown" by the pull. Say it here.
        #
        # It cannot be supported rather than rejected: this override names a FAMILY, and the
        # members (robovast, robovast-roqsim, robovast-sidecar) are separate images with
        # separate digests. A tag is the only thing they can share.
        if '@' in args.image:
            raise SystemExit(
                f"--image takes a tag, not a digest, got '{args.image}'.\n"
                "  It sets ROBOVAST_PROJECT + ROBOVAST_PROJECT_TAG for the whole image family,\n"
                "  whose members are distinct images -- no one digest names the set.")
        project, _, tag = args.image.rpartition(':')
        if not project or not tag:
            raise SystemExit(
                f"--image must be <project>/<member>:<tag>, got '{args.image}'")
        project = project.rsplit('/', 1)[0]
        if not project:
            raise SystemExit(
                f"--image needs a project before the member, got '{args.image}'")
        os.environ['ROBOVAST_PROJECT'] = project
        os.environ['ROBOVAST_PROJECT_TAG'] = tag
        print(f"Image family: {project}/<member>:{tag}")

    global _CLUSTER  # pylint: disable=global-statement
    if not (args.data_root and args.data_mount):
        raise SystemExit("--context needs --data-root and --data-mount")
    _CLUSTER = ClusterSession(repo_root, args.test_directory, args.context,
                              args.namespace, args.data_root, args.data_mount,
                              args.cluster_config)
    print(f"Service: deployed into context {args.context}, namespace {args.namespace}")

    tests = [
        ("Complete workflow: init -> execution -> postprocess", test_vast_workflow, args.vast_file, args.test_directory, args.config, args.runs),
    ]
    if not args.no_packing_test:
        tests.append(
            ("runs_per_job packing equivalence", test_runs_per_job_packing,
             args.vast_file, args.test_directory, args.config, args.runs)
        )

    results = []
    try:
        for name, test_func, *test_args in tests:
            try:
                result = test_func(*test_args)
                results.append((name, bool(result)))
            except Exception as e:
                print(f"✗ Test '{name}' raised exception: {e}")
                traceback.print_exc()
                results.append((name, False))
    finally:
        if _CLUSTER is not None:
            _CLUSTER.close()

    print("\n" + "="*60)
    print("Test Results Summary")
    print("="*60)
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {name}")

    all_passed = all(result for _, result in results)
    print("="*60)
    if all_passed:
        print("✓ All tests passed!")
        return 0
    else:
        print("✗ Some tests failed!")
        return 1


if __name__ == '__main__':
    sys.exit(main())
