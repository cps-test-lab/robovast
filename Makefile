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
	@find . -name "*.py" -not -path "./dependencies/*" -not -path "./venv/*" -not -path "./.venv/*" -not -path "./install/*" -not -path "./build/*" | xargs pylint --rcfile=.github/linters/.pylintrc

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
	. venv/bin/activate && pip install -e .[docs] && pip install -e src/robovast_nav
	@touch venv/.robovast_installed

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