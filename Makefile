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
	@find . -name "*.py" -not -path "./venv/*" -not -path "./.venv/*" -not -path "./install/*" -not -path "./build/*" | xargs pylint --rcfile=.github/linters/.pylintrc

.PHONY: fix
fix: check-tools
	@echo "Auto-fixing Python code..."
	@echo "Running autoflake..."
	@autoflake --in-place --remove-all-unused-imports --recursive .
	@echo "Running isort..."
	@isort .
	@echo "Running autopep8..."
	@autopep8 --in-place --recursive --max-line-length 140 .
	@echo "✅ Done! Now run 'make check' to verify."