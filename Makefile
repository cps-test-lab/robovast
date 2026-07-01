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

venv/.robovast_installed: 
	@if [ ! -d venv ]; then \
		echo "Creating virtual environment..."; \
		python3 -m venv venv; \
	fi
	
	@echo "Setting up RoboVAST environment..."
	. venv/bin/activate && pip install -e .[docs,test,gui] && pip install -e src/robovast_nav

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

.PHONY: build
build:
	poetry build
	cd src/robovast_nav && poetry build

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
