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
# sentinel alone lets an existing venv silently keep whatever was installed the day it
# was made: adding a distribution strands every developer's environment without a lane
# and without a word, while `make venv` cheerfully reports nothing to do.
# Makefile itself is a prerequisite: the recipe lives here, so changing *how* the venv is
# built must re-run it too. Listing only the manifests makes a fix to the install order a
# no-op for everyone who already has a venv -- the same silence, one level up.
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
	# with no execution lane but `local`, so `vast cluster` disappears and the
	# cross-lane tests fail on a missing plugin -- the same shape as the roqsim miss above.
	# robovast-client goes LAST, and that ordering is load-bearing. It is a non-optional
	# path dependency of robovast, so `pip install -e .` resolves it and installs a plain
	# *copy* into site-packages -- silently replacing an editable install done earlier.
	# The result is a developer editing src/robovast_client and seeing no effect, with
	# nothing said. Installing it after everything that depends on it is what makes the
	# editable install the one that survives.
	# `notebooks` is named even though the `test` extra happens to carry the same five
	# packages: a dev venv runs `vast serve`, and the Explorer's notebook endpoint is
	# 503 without that toolchain. Depending on the test extra for it meant the service's
	# capability rode on why the *suite* needs nbformat -- a coincidence, and one that
	# reads like an accident the moment either list is edited.
	. venv/bin/activate && pip install -e .[docs,test,notebooks] \
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

# venv/.robovast_installed, not a docs-only sentinel: `pip install -e .[docs,test]`
# above already brings in sphinxcontrib-spelling, so there is no separate docs install
# to name -- and a sentinel with no rule leaves the target unable to run at all.
checkspelling: venv/.robovast_installed
	. venv/bin/activate && GITHUB_REF_NAME=local GITHUB_REPOSITORY=cps-test-lab/robovast python3 -m sphinx -b html -b spelling -W docs $(LINKCHECKDIR)
	@echo
	@echo "Check finished. Report is in $(LINKCHECKDIR)."

poetry_reinstall:
	@echo "Reinstalling all Poetry dependencies..."
	poetry env remove python || true
	rm poetry.lock || true >/dev/null 2>&1 
	poetry install
	@echo "✅ Done!"

# The Python suite. A target rather than a line in a CI workflow, because the workflow is
# not somewhere a developer looks -- and a component whose tests are only described there
# drifts from the command that actually gates it. CI calls this, so the two cannot.
#
# One `tests/` tree in one process, unlike roqsim's parallel run: nothing here imports a
# simulator, so there is no memory peak to bound and a JOBS knob would be a guess.
.PHONY: test
test: venv/.robovast_installed ## Run the Python unit tests (what CI runs)
	. venv/bin/activate && pytest tests/ -q

# One subtree, e.g. `make test-service`. How to re-run just what you are editing without
# paying for the rest. (The frontend's vitest is its own: `cd frontend/ui && npm run test`.)
.PHONY: test-%
test-%: venv/.robovast_installed
	. venv/bin/activate && pytest tests/$* -q

# Two package trees have to agree on one Module-Federation runtime version, and neither
# tree can check that alone -- so it lives here rather than in `frontend/ui`'s vitest.
.PHONY: check-mf-runtime
check-mf-runtime:
	@python3 tools/check_mf_runtime.py

.PHONY: refresh-build-pins
refresh-build-pins: ## Re-resolve base-image digests and the dated apt archives (asks first; WRITE=1 to skip the question)
	@python3 tools/refresh_build_pins.py $(if $(WRITE),--write,--ask)

# The source-side counterpart of the target above, separate because the two refresh different kinds
# of ground: that one takes whatever a third party published, this one moves the image onto a new
# commit of code we write, which is a release decision and wants its own diff. BRANCH= to resolve
# something other than main.
#
# Named after `release-images` rather than after its sibling above, because it is the release flow
# it belongs to: what these pins decide is which sources that command bakes. One name for it, and
# the same one a superproject holding these sources exposes -- two names for one target is how a
# caller learns the wrong one.
#
# It refreshes the pins and nothing more. A superproject that holds these sources as submodules can
# additionally compare each pin against the checkout sitting beside it -- which this repo cannot do,
# having no idea those checkouts exist.
.PHONY: release-images-update-versions
release-images-update-versions: ## Move the commits release-images bakes (roqsim, scenario-execution) onto their branch heads (asks first; WRITE=1 to skip the question)
	@python3 tools/refresh_source_pins.py $(if $(WRITE),--write,--ask) $(if $(BRANCH),--branch $(BRANCH),)

.PHONY: new-config-migration
new-config-migration: ## Scaffold a .vast config migration step (see migrations/README.md)
	@python3 tools/new_config_migration.py

.PHONY: config-fields
config-fields: ## Regenerate compat/config_fields.json from the config models
	@python3 tools/config_fields.py --write

.PHONY: check-config-fields
check-config-fields: ## Fail if compat/config_fields.json is out of date with the models
	@python3 tools/config_fields.py --check

.PHONY: check-config-version
check-config-version: ## Fail if a config version bump is missing, or unnecessary
	@python3 tools/check_config_version.py $(if $(BASE),--base $(BASE),)

.PHONY: check-compat-version
check-compat-version: ## Fail if the host<->container contract changed without a version bump
	@python3 tools/check_compat_version.py $(if $(BASE),--base $(BASE),)

# The lock is what the controller image installs from -- it COPYs pyproject.toml and
# poetry.lock together and runs `poetry install`, which refuses the pair outright when the
# hash disagrees. So an unrelocked pyproject fails minutes into an image build instead of
# next to the edit. Pinned to the version that consumes it, since a check by a different
# poetry is not the check that matters.
.PHONY: check-lock
check-lock: ## Fail if poetry.lock does not describe pyproject.toml
	@command -v poetry >/dev/null 2>&1 || { echo "❌ poetry is not installed. Install with: pip install poetry==1.8.2"; exit 1; }
	@poetry check --lock || { echo ""; echo "Fix with: poetry lock --no-update"; exit 1; }

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
	@test -n "$(PROJECT)" || { echo "Usage: make release-images PROJECT=docker.io/<namespace> [TAG=<tag>] [PUSH=1] [ROQSIM_REF=<ref> | ROQSIM_SRC=<path>] [ROS_DISTRO=<distro>]"; echo "Publishes all four family images (robovast, robovast-roqsim, robovast-controller, robovast-sidecar) under one tag, and prints the two lines that configure them: ROBOVAST_PROJECT and ROBOVAST_PROJECT_TAG."; echo "TAG defaults to latest, which floats. Pass TAG=\$$(date +%F) to publish an immutable set -- one tag covers the whole family, so a tag is what pins a deployment."; echo "PUSH=1 publishes without asking; without it you are asked before the first build, and answering no builds without publishing."; echo "ROQSIM_REF pins which roqsim commit is cloned into the simulator image; ROQSIM_SRC builds it from a checkout on disk instead. Without either, the script's default branch is used."; exit 1; }
	./container/release_images.sh --project "$(PROJECT)" $(if $(PUSH),--push,--ask-push) \
		$(if $(TAG),--tag "$(TAG)",) \
		$(if $(ROQSIM_REF),--roqsim-ref "$(ROQSIM_REF)",) \
		$(if $(ROQSIM_SRC),--roqsim-src "$(ROQSIM_SRC)",) \
		$(if $(ROS_DISTRO),--ros-distro "$(ROS_DISTRO)",)

# One family member at a time -- the targets the dev loop uses.
#
# `release-images` above stays the RELEASE path and publishes all four, because
# ROBOVAST_PROJECT moves all four. These two exist because iterating does not need that:
# `vast service upgrade` rolls only robovast-controller, and the other three members are
# pulled by campaign job pods. So a change to robovast's own source needs the controller image
# and nothing else, and a roqsim change needs robovast-roqsim and nothing else -- which is the
# difference between publishing 0.56 GB and publishing 4.35 GB.
#
# The cost of that, and the reason both usage texts say so: a partial publish is only valid when
# the other members ALREADY exist at this PROJECT/TAG, since ROBOVAST_PROJECT resolves the family
# as a set. `make image-digests PROJECT=... TAG=...` is the check for it.
.PHONY: release-image-controller
release-image-controller:
	@test -n "$(PROJECT)" || { echo "Usage: make release-image-controller PROJECT=docker.io/<namespace> [TAG=<tag>] [PUSH=1]"; echo "Builds ONLY robovast-controller -- the one image 'vast service upgrade' rolls. Use it for a change to robovast's own Python source."; echo "The other three family members must already exist at this PROJECT/TAG; 'make image-digests' is the check."; echo "PUSH=1 publishes without asking; without it you are asked before the build."; exit 1; }
	./container/controller/build.sh \
		-t "$(patsubst %/,%,$(PROJECT))/robovast-controller:$(if $(TAG),$(TAG),latest)" \
		$(if $(PUSH),--push,--ask-push)

# ROQSIM_REF is passed in the ENVIRONMENT rather than as a flag, because
# container/robovast/build.sh has no --roqsim-ref option and reads it from there -- where it
# resolves it to a sha before building, which is what keeps the clone layer's cache key honest.
# release_images.sh passes it the same way; a flag here would be silently ignored.
.PHONY: release-image-roqsim
release-image-roqsim:
	@test -n "$(PROJECT)" || { echo "Usage: make release-image-roqsim PROJECT=docker.io/<namespace> [TAG=<tag>] [PUSH=1] [ROQSIM_SRC=<path> | ROQSIM_REF=<ref>] [ROS_DISTRO=<distro>]"; echo "Builds ONLY robovast-roqsim. It is FROM <PROJECT>/robovast:<TAG>, so that base must already be published there -- the build fails loudly if it is not."; echo "The rest of the family must already exist at this PROJECT/TAG; 'make image-digests' is the check."; echo "PUSH=1 publishes without asking; without it you are asked before the build."; exit 1; }
	$(if $(ROQSIM_REF),ROQSIM_REF="$(ROQSIM_REF)",) ./container/robovast/build.sh --image roqsim \
		--project "$(PROJECT)" --tag "$(if $(TAG),$(TAG),latest)" \
		$(if $(ROS_DISTRO),--ros-distro "$(ROS_DISTRO)",) \
		$(if $(ROQSIM_SRC),--roqsim-src "$(ROQSIM_SRC)",) \
		$(if $(PUSH),--push,--ask-push)

.PHONY: image-digests
image-digests:
	@test -n "$(PROJECT)" || { echo "Usage: make image-digests PROJECT=docker.io/<namespace> [TAG=<tag>]"; echo "Reports whether that project holds a complete, pullable family at that tag, and what each member currently resolves to. Fails if any member is missing -- ROBOVAST_PROJECT moves all four at once, so a partial set cannot serve a campaign."; echo "Builds nothing: it reads the registry, so it works on a machine that has never built an image."; exit 1; }
	@./container/image_digests.sh --project "$(PROJECT)" \
		$(if $(TAG),--tag "$(TAG)",)

.PHONY: build-client
build-client:
	cd src/robovast_client && poetry build

# TestPyPI refuses to re-upload a filename it already holds, so a second rehearsal of the
# same version is a 400. It does NOT build-client: the rehearsal is stamped with a
# post-release of the tree's version (2.1.0, then 2.1.0.post1, .post2, ...) so it can be
# repeated as often as the packaging claim needs, and pyproject.toml keeps the version
# that will reach real PyPI rather than one inflated by rehearsals. The stamp is restored
# on the way out, including on failure -- see tools/next_testpypi_version.py for why it is
# a post-release and not a .devN.
# DRY_RUN=1 stamps and builds but uploads nothing.
.PHONY: publish-client-test
publish-client-test:
	@echo "Publishing robovast-client to TestPyPI..."
	@echo "💡 If this fails with 403, run: poetry config pypi-token.testpypi pypi-<your-token>"
	@cd src/robovast_client && \
		base=$$(poetry version -s) && \
		stamp=$$(python3 ../../tools/next_testpypi_version.py robovast-client "$$base") && \
		echo "Rehearsing as $$stamp; pyproject.toml stays at $$base." && \
		trap 'poetry version "$$base" >/dev/null' EXIT && \
		poetry version "$$stamp" >/dev/null && \
		poetry build && \
		poetry publish --repository testpypi $(if $(DRY_RUN),--dry-run,)

# Where the packaging claim gets tested, from a real wheel rather than a source tree.
# `--help` exiting 0 is not enough: the distribution's whole point is what it does NOT
# drag in, and a stray module-level import would reintroduce the weight silently.
.PHONY: publish-client-test-venv
publish-client-test-venv:
	@echo "Installing ONLY robovast-client from TestPyPI, in a fresh venv..."
	rm -rf /tmp/robovast-client-test-venv
	python3 -m venv /tmp/robovast-client-test-venv
# --no-cache-dir because pip caches the index page: without it, a rehearsal minutes after
# its own upload resolves to the version cached BEFORE it and tests a wheel nobody just
# built. That is not hypothetical -- it is how this target once reported a missing verb
# against a stale release while the fresh one sat on the index unread.
	/tmp/robovast-client-test-venv/bin/pip install \
		--no-cache-dir \
		--index-url https://test.pypi.org/simple/ \
		--extra-index-url https://pypi.org/simple/ \
		robovast-client
	@echo "The surface is the client's, and nothing else..."
# The VERB LIST, not the help text. Grepping the whole `--help` for "^  <verb>" reads the
# prose too, so an absence check for `config` once matched a documentation paragraph and
# failed a perfectly good install. Only the `Commands:` section is a list of verbs, and
# only its first column is a verb name -- so extract that and compare whole lines.
	@/tmp/robovast-client-test-venv/bin/vast --help > /tmp/robovast-client-help.txt
	@awk '/^Commands:/{f=1;next} f && /^  [^ ]/{print $$1}' \
		/tmp/robovast-client-help.txt | sort -u > /tmp/robovast-client-verbs.txt
	@for verb in login logout workspace campaign service cluster container files doctor image; do \
		if ! grep -qx "$$verb" /tmp/robovast-client-verbs.txt; then \
			echo "❌ '$$verb' missing from vast --help"; exit 1; fi; \
	done
# `if`, not `grep && { exit 1; }`. The latter returns GREP's status, so an absence loop
# whose last verb is correctly absent exits 1 and fails the target on the passing path --
# which is why this check had never once run green.
#
# `wait`, `service-log` and `exec` are absent because they MOVED, not because they need the
# core: `campaign wait`, `service log`, `container exec`. Asserting their absence is what
# stops a stale alias creeping back in.
	@for verb in serve init config results ui import-results wait service-log exec; do \
		if grep -qx "$$verb" /tmp/robovast-client-verbs.txt; then \
			echo "❌ '$$verb' present in a client-only install"; exit 1; fi; \
	done
# A group being listed is not the whole claim -- it must resolve to the verb a user types.
# Checked by RUNNING each `--help`, because a group lists a subcommand it cannot load and
# still exits 0 on its own `--help`.
	@echo "...that the launch verb resolves, and the groups stop short of the operator's..."
	@/tmp/robovast-client-test-venv/bin/vast workspace run --help > /dev/null \
		|| { echo "❌ 'vast workspace run' does not resolve on a client-only install"; exit 1; }
	@for verb in validate preview init update; do \
		/tmp/robovast-client-test-venv/bin/vast workspace $$verb --help > /dev/null \
			|| { echo "❌ 'vast workspace $$verb' does not resolve"; exit 1; }; \
	done
	@for verb in wait status list stop stop-job log rerun download; do \
		/tmp/robovast-client-test-venv/bin/vast campaign $$verb --help > /dev/null \
			|| { echo "❌ 'vast campaign $$verb' does not resolve"; exit 1; }; \
	done
	@for verb in log info resources restart; do \
		/tmp/robovast-client-test-venv/bin/vast service $$verb --help > /dev/null \
			|| { echo "❌ 'vast service $$verb' does not resolve"; exit 1; }; \
	done
	@/tmp/robovast-client-test-venv/bin/vast cluster store-cleanup --help > /dev/null \
		|| { echo "❌ 'vast cluster store-cleanup' does not resolve"; exit 1; }
	@for verb in exec stop; do \
		/tmp/robovast-client-test-venv/bin/vast container $$verb --help > /dev/null \
			|| { echo "❌ 'vast container $$verb' does not resolve"; exit 1; }; \
	done
# The halves that need a kubeconfig must NOT be here. `service` and `cluster` each span two
# distributions on purpose -- the group is named after its object, not after the install --
# so what must hold is that this install is short the operator verbs, not short the group.
	@/tmp/robovast-client-test-venv/bin/vast cluster --help \
		| awk '/^Commands:/{f=1;next} f && /^  [^ ]/{print $$1}' | sort -u \
		> /tmp/robovast-client-cluster-verbs.txt
	@for verb in setup cleanup jobs-cleanup monitor; do \
		if grep -qx "$$verb" /tmp/robovast-client-cluster-verbs.txt; then \
			echo "❌ 'cluster $$verb' present without robovast-cluster"; exit 1; fi; \
	done
	@/tmp/robovast-client-test-venv/bin/vast service --help \
		| awk '/^Commands:/{f=1;next} f && /^  [^ ]/{print $$1}' | sort -u \
		> /tmp/robovast-client-service-verbs.txt
	@for verb in upgrade token; do \
		if grep -qx "$$verb" /tmp/robovast-client-service-verbs.txt; then \
			echo "❌ 'service $$verb' present without robovast-cluster"; exit 1; fi; \
	done
# Launching moved out of every old path; nothing may answer to it there.
	@for group in cluster service container; do \
		if /tmp/robovast-client-test-venv/bin/vast $$group run --help > /dev/null 2>&1; then \
			echo "❌ '$$group run' still resolves; the launch verb is 'workspace run'"; exit 1; fi; \
	done
# `--version` must not name the `robovast` distribution, which a client-only install does
# not have -- click resolves that lazily, so it raises only when asked. Cheap to assert.
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

# Post-release stamped and repeatable, for the same reason publish-client-test is; see the
# comment there and tools/next_testpypi_version.py. Each distribution gets its own number
# because each has its own TestPyPI history -- they share a version in the tree, not
# necessarily on the index. It does NOT depend on `build`, which builds all four at the
# tree's plain version; only the two published here are stamped and built.
#
# robovast first, then robovast-nav, which requires it: nav's `robovast = "^2.0.0"` is a
# range, so it accepts the stamped robovast, but the release has to be on the index by the
# time nav's install is resolved.
#
# DRY_RUN=1 stamps and builds but uploads nothing -- the check for "what would this
# publish, and does the stamp come back off afterwards?" that costs no version.
.PHONY: publish-test
publish-test: ui-stage
	@echo "💡 If this fails with 403, run: poetry config pypi-token.testpypi pypi-<your-token>"
	@set -e; for spec in "robovast:." "robovast-nav:src/robovast_nav"; do \
		dist=$${spec%%:*}; dir=$${spec#*:}; \
		echo "Publishing $$dist to TestPyPI..."; \
		( cd "$$dir" && \
			base=$$(poetry version -s) && \
			stamp=$$($(CURDIR)/tools/next_testpypi_version.py "$$dist" "$$base") && \
			echo "Rehearsing as $$stamp; pyproject.toml stays at $$base." && \
			trap 'poetry version "$$base" >/dev/null' EXIT && \
			poetry version "$$stamp" >/dev/null && \
			poetry build && \
			poetry publish --repository testpypi $(if $(DRY_RUN),--dry-run,) ); \
	done


.PHONY: publish-test-venv
publish-test-venv:
	@echo "Testing install from TestPyPI in a fresh venv..."
	rm -rf /tmp/robovast-test-venv
	python3 -m venv /tmp/robovast-test-venv
# --no-cache-dir for the reason publish-client-test-venv carries it: pip's cached index
# page outlives the upload, and the rehearsal would test the release before this one.
	/tmp/robovast-test-venv/bin/pip install \
		--no-cache-dir \
		--index-url https://test.pypi.org/simple/ \
		--extra-index-url https://pypi.org/simple/ \
		robovast robovast-nav
	@echo "Testing vast CLI..."
	/tmp/robovast-test-venv/bin/vast --help
	@echo "✅ Install from TestPyPI succeeded!"
