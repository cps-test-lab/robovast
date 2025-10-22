
LINKCHECKDIR  = build/linkcheck

sphinx_setup:
	if [ ! -d "venv" ]; then \
		python -m venv venv/; \
		. venv/bin/activate; \
		pip install -r docs/requirements.txt; \
		deactivate; \
	fi

doc: sphinx_setup checklinks checkspelling
	. venv/bin/activate && GITHUB_REF_NAME=local GITHUB_REPOSITORY=cps-test-lab/robovast python -m sphinx -b html -W docs build/html

view_doc: doc
	firefox build/html/index.html &

checklinks: sphinx_setup
	. venv/bin/activate && GITHUB_REF_NAME=local GITHUB_REPOSITORY=cps-test-lab/robovast python -m sphinx -b linkcheck -W docs $(LINKCHECKDIR)
	@echo
	@echo "Check finished. Report is in $(LINKCHECKDIR)."

checkspelling: sphinx_setup
	. venv/bin/activate && GITHUB_REF_NAME=local GITHUB_REPOSITORY=cps-test-lab/robovast python -m sphinx -b html -b spelling -W docs $(LINKCHECKDIR)
	@echo
	@echo "Check finished. Report is in $(LINKCHECKDIR)."
