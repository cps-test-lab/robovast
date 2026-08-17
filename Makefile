LINKCHECKDIR  = build/linkcheck

.PHONY: check-tools
check-tools:
	@echo "Checking for required tools..."
	@command -v pylint >/dev/null 2>&1 || { echo "❌ pylint is not installed. Install with: pip install pylint"; exit 1; }
	@command -v isort >/dev/null 2>&1 || { echo "❌ isort is not installed. Install with: sudo apt install isort"; exit 1; }
	@command -v autopep8 >/dev/null 2>&1 || { echo "❌ autopep8 is not installed. Install with: pip install python3-autopep8"; exit 1; }
	@command -v autoflake >/dev/null 2>&1 || { echo "⚠️  autoflake is not installed (optional). Install with: pip install autoflake"; }

.PHONY: install-tools
install-tools:
	@echo "Installing Python linting and formatting tools..."
	pip install pylint isort black autopep8 autoflake
	@echo "✅ All tools installed!"

.PHONY: check
check: check-tools
	@echo "Running pylint..."
	@find . -type d \( -name "venv" -o -name ".venv" -o -name "dependencies" -o -name "install" -o -name "build" \) -prune -o -name "*.py" -print | xargs pylint --rcfile=.github/linters/.pylintrc

.PHONY: fix
fix: check-tools
	@echo "Auto-fixing Python code..."
	@echo "Running autoflake..."
	@autoflake --in-place --remove-all-unused-imports --recursive --exclude venv,.venv,dependencies,install,build .
	@echo "Running isort..."
	@isort . --skip venv --skip .venv --skip dependencies --skip install --skip build
	@echo "Running autopep8..."
	@autopep8 --in-place --recursive --max-line-length 140 --exclude venv,.venv,dependencies,install,build .
	@echo "✅ Done! Now run 'make check' to verify."

.PHONY: venv
venv: venv/.robovast_installed

# Re-run when any distribution's manifest changes, not only when venv/ is absent. The
# sentinel alone meant an existing venv silently kept whatever was installed the day it
# was made: adding robovast-cluster stranded every developer's environment without a lane
# and without a word, and `make venv` cheerfully reported nothing to do.
# Makefile itself is a prerequisite: the recipe lives here, so changing *how* the venv is
# built must re-run it too. Listing only the manifests meant a fix to the install order
# was a no-op for everyone who already had a venv -- the same silence the sentinel alone
# used to cause.
venv/.robovast_installed: Makefile pyproject.toml src/robovast_nav/pyproject.toml \
                          src/robovast_sim_roqsim/pyproject.toml \
                          src/robovast_cluster/pyproject.toml \
                          src/robovast_client/pyproject.toml
	@if [ ! -d venv ]; then \
		echo "Creating virtual environment..."; \
		python3 -m venv venv; \
	fi
	
	@echo "Setting up RoboVAST environment..."
	# The sibling packages are installed explicitly, not via extras: they are path
	# dependencies, and `pip install -e .[roqsim]` would take them from the index.
	# roqsim was missing here, so every fresh venv lacked the `roqsim` simulator entry
	# point and ~25 tests failed on "Unknown robovast.simulators plugin" -- a broken
	# environment that looked like broken code.
	# robovast-cluster is a distribution, not an extra: `pip install -e .` yields a core
	# with no execution lane but `local`, so `vast exec cluster` disappears and the
	# cross-lane tests fail on a missing plugin -- the same shape as the roqsim miss above.
	# robovast-client goes LAST, and that ordering is load-bearing. It is a non-optional
	# path dependency of robovast, so `pip install -e .` resolves it and installs a plain
	# *copy* into site-packages -- silently replacing an editable install done earlier.
	# The result is a developer editing src/robovast_client and seeing no effect, with
	# nothing said. Installing it after everything that depends on it is what makes the
	# editable install the one that survives.
	. venv/bin/activate && pip install -e .[docs,test] \
		&& pip install -e src/robovast_nav \
		&& pip install -e src/robovast_sim_roqsim \
		&& pip install -e src/robovast_cluster \
		&& pip install -e src/robovast_client

	@touch venv/.robovast_installed
	@echo ""
	@echo "✅ Virtual environment created successfully!"
	@echo "To activate the virtual environment, run:"
	@echo "  source venv/bin/activate"

doc: venv/.robovast_installed
	. venv/bin/activate && GITHUB_REF_NAME=local GITHUB_REPOSITORY=cps-test-lab/robovast python3 -m sphinx -b html -W docs build/html

view_doc: doc
	firefox build/html/index.html &

checklinks: venv
	. venv/bin/activate && GITHUB_REF_NAME=local GITHUB_REPOSITORY=cps-test-lab/robovast python3 -m sphinx -b linkcheck -W docs $(LINKCHECKDIR)
	@echo
	@echo "Check finished. Report is in $(LINKCHECKDIR)."

checkspelling: venv/.docs_installed
	. venv/bin/activate && GITHUB_REF_NAME=local GITHUB_REPOSITORY=cps-test-lab/robovast python3 -m sphinx -b html -b spelling -W docs $(LINKCHECKDIR)
	@echo
	@echo "Check finished. Report is in $(LINKCHECKDIR)."

poetry_reinstall:
	@echo "Reinstalling all Poetry dependencies..."
	poetry env remove python || true
	rm poetry.lock || true >/dev/null 2>&1 
	poetry install
	@echo "✅ Done!"

# Two package trees have to agree on one Module-Federation runtime version, and neither
# tree can check that alone -- so it lives here rather than in `frontend/ui`'s vitest.
.PHONY: check-mf-runtime
check-mf-runtime:
	@python3 tools/check_mf_runtime.py

.PHONY: ui-stage
ui-stage: check-mf-runtime ## Copy the built web UI into the package so the wheel carries it
	@test -f frontend/ui/dist/index.html || { echo "frontend/ui/dist is not built. Run: cd frontend/ui && npm ci && npm run build"; exit 1; }
	rm -rf src/robovast/_ui
	cp -r frontend/ui/dist src/robovast/_ui
	@echo "staged the web UI into src/robovast/_ui for the wheel"

.PHONY: build
build: ui-stage
	poetry build
	cd src/robovast_nav && poetry build
	cd src/robovast_cluster && poetry build
	cd src/robovast_client && poetry build

.PHONY: release-images
release-images:
	@test -n "$(PROJECT)" || { echo "Usage: make release-images PROJECT=docker.io/<namespace> [TAG=<tag>] [PUSH=1] [ROQSIM_REF=<ref> | ROQSIM_SRC=<path>] [ROS_DISTRO=<distro>]"; echo "Publishes all four family images (robovast, robovast-roqsim, robovast-controller, robovast-sidecar) under one tag, and prints the two lines that configure them: ROBOVAST_PROJECT and ROBOVAST_PROJECT_TAG."; echo "TAG defaults to latest, which floats. Pass TAG=\$$(date +%F) to publish an immutable set -- one tag covers the whole family, so a tag is what pins a deployment."; echo "PUSH=1 actually publishes; without it nothing reaches the registry."; echo "ROQSIM_REF pins which roqsim commit is cloned into the simulator image; ROQSIM_SRC builds it from a checkout on disk instead. Without either, the script's default branch is used."; exit 1; }
	./container/release_images.sh --project "$(PROJECT)" $(if $(PUSH),--push,) \
		$(if $(TAG),--tag "$(TAG)",) \
		$(if $(ROQSIM_REF),--roqsim-ref "$(ROQSIM_REF)",) \
		$(if $(ROQSIM_SRC),--roqsim-src "$(ROQSIM_SRC)",) \
		$(if $(ROS_DISTRO),--ros-distro "$(ROS_DISTRO)",)

.PHONY: image-digests
image-digests:
	@test -n "$(PROJECT)" || { echo "Usage: make image-digests PROJECT=docker.io/<namespace> [TAG=<tag>]"; echo "Reports whether that project holds a complete, pullable family at that tag, and what each member currently resolves to. Fails if any member is missing -- ROBOVAST_PROJECT moves all four at once, so a partial set cannot serve a campaign."; echo "Builds nothing: it reads the registry, so it works on a machine that has never built an image."; exit 1; }
	@./container/image_digests.sh --project "$(PROJECT)" \
		$(if $(TAG),--tag "$(TAG)",)

.PHONY: build-client
build-client:
	cd src/robovast_client && poetry build

.PHONY: publish-client-test
publish-client-test: build-client
	@echo "Publishing robovast-client to TestPyPI..."
	@echo "💡 If this fails with 403, run: poetry config pypi-token.testpypi pypi-<your-token>"
	cd src/robovast_client && poetry publish --repository testpypi

# Where the packaging claim gets tested, from a real wheel rather than a source tree.
# `--help` exiting 0 is not enough: the distribution's whole point is what it does NOT
# drag in, and a stray module-level import would reintroduce the weight silently.
.PHONY: publish-client-test-venv
publish-client-test-venv:
	@echo "Installing ONLY robovast-client from TestPyPI, in a fresh venv..."
	rm -rf /tmp/robovast-client-test-venv
	python3 -m venv /tmp/robovast-client-test-venv
	/tmp/robovast-client-test-venv/bin/pip install \
		--index-url https://test.pypi.org/simple/ \
		--extra-index-url https://pypi.org/simple/ \
		robovast-client
	@echo "The surface is the client's, and nothing else..."
# The VERB LIST, not the help text. Grepping the whole `--help` for "^  <verb>" reads the
# prose too: the `--vast-file` paragraph wraps onto a line beginning "  configuration file
# instead of...", so an absence check for `config` matched documentation and failed a
# perfectly good install. Only the `Commands:` section is a list of verbs, and only its
# first column is a verb name -- so extract that and compare whole lines.
	@/tmp/robovast-client-test-venv/bin/vast --help > /tmp/robovast-client-help.txt
	@awk '/^Commands:/{f=1;next} f && /^  [^ ]/{print $$1}' \
		/tmp/robovast-client-help.txt | sort -u > /tmp/robovast-client-verbs.txt
	@for verb in login logout workspace files wait doctor image exec; do \
		if ! grep -qx "$$verb" /tmp/robovast-client-verbs.txt; then \
			echo "❌ '$$verb' missing from vast --help"; exit 1; fi; \
	done
# `if`, not `grep && { exit 1; }`. The latter returns GREP's status, so an absence loop
# whose last verb is correctly absent exits 1 and fails the target on the passing path --
# which is why this check had never once run green.
	@for verb in serve init config results ui import-results; do \
		if grep -qx "$$verb" /tmp/robovast-client-verbs.txt; then \
			echo "❌ '$$verb' present in a client-only install"; exit 1; fi; \
	done
# `exec` being present is not the whole claim -- it must resolve down two lazy levels to
# the verb a user actually types, and must NOT expose the halves that need Docker or a
# kubeconfig. `run --help` is checked by RUNNING it, because a group lists a subcommand it
# cannot load and still exits 0 on its own `--help`.
	@echo "...that 'exec' reaches the launch verb, and stops short of the operator's..."
	@/tmp/robovast-client-test-venv/bin/vast exec cluster run --help > /dev/null \
		|| { echo "❌ 'vast exec cluster run' does not resolve on a client-only install"; exit 1; }
	@/tmp/robovast-client-test-venv/bin/vast exec --help \
		| awk '/^Commands:/{f=1;next} f && /^  [^ ]/{print $$1}' | sort -u \
		> /tmp/robovast-client-exec-verbs.txt
	@if grep -qx "local" /tmp/robovast-client-exec-verbs.txt; then \
		echo "❌ 'exec local' present without the core (it needs Docker)"; exit 1; fi
	@/tmp/robovast-client-test-venv/bin/vast exec cluster --help \
		| awk '/^Commands:/{f=1;next} f && /^  [^ ]/{print $$1}' | sort -u \
		> /tmp/robovast-client-cluster-verbs.txt
	@for verb in run stop stop-job log download-cleanup; do \
		if ! grep -qx "$$verb" /tmp/robovast-client-cluster-verbs.txt; then \
			echo "❌ 'exec cluster $$verb' missing from a client-only install"; exit 1; fi; \
	done
	@for verb in setup cleanup upgrade token monitor run-cleanup; do \
		if grep -qx "$$verb" /tmp/robovast-client-cluster-verbs.txt; then \
			echo "❌ 'exec cluster $$verb' present without robovast-cluster"; exit 1; fi; \
	done
# `--version` used to name the `robovast` distribution, which a client-only install does
# not have -- click resolves that lazily, so it raised only when asked. Cheap to assert.
	@/tmp/robovast-client-test-venv/bin/vast --version > /dev/null \
		|| { echo "❌ 'vast --version' fails on a client-only install"; exit 1; }
	@echo "...and none of the weight it exists to avoid."
	@/tmp/robovast-client-test-venv/bin/python -c "import importlib.util as u, sys; \
		heavy = [m for m in ('numpy','pandas','fastapi','kubernetes','docker', \
		'scenario_execution','matplotlib') if u.find_spec(m)]; \
		sys.exit(f'❌ client install pulled {heavy}') if heavy else None"
	@echo "✅ robovast-client installs clean from TestPyPI."

# A PyPI upload cannot be replaced -- a version can be yanked, never re-uploaded -- so
# this refuses unless asked twice, and refuses a dirty tree outright: the artifact would
# correspond to no commit.
.PHONY: publish-client
publish-client: build-client
	@test -z "$$(git status --porcelain)" \
		|| { echo "❌ working tree is dirty; the wheel would match no commit"; exit 1; }
	@test "$(CONFIRM)" = "1" || { \
		echo "This publishes robovast-client $$(cd src/robovast_client && poetry version -s) to PyPI, permanently."; \
		echo "A version can be yanked but never re-uploaded. Re-run with CONFIRM=1 when you mean it."; \
		echo "Nothing should reach here that has not been through 'make publish-client-test-venv'."; \
		exit 1; }
	cd src/robovast_client && poetry publish

.PHONY: publish-test
publish-test: build
	@echo "Publishing robovast to TestPyPI..."
	@echo "💡 If this fails with 403, run: poetry config pypi-token.testpypi pypi-<your-token>"
	poetry publish --repository testpypi
	@echo "Publishing robovast-nav to TestPyPI..."
	cd src/robovast_nav && poetry publish --repository testpypi


.PHONY: publish-test-venv
publish-test-venv:
	@echo "Testing install from TestPyPI in a fresh venv..."
	rm -rf /tmp/robovast-test-venv
	python3 -m venv /tmp/robovast-test-venv
	/tmp/robovast-test-venv/bin/pip install \
		--index-url https://test.pypi.org/simple/ \
		--extra-index-url https://pypi.org/simple/ \
		robovast robovast-nav
	@echo "Testing vast CLI..."
	/tmp/robovast-test-venv/bin/vast --help
	@echo "✅ Install from TestPyPI succeeded!"
