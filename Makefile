VENV := .venv
PY := $(VENV)/bin/python
ALLURE_RESULTS := allure-results
ALLURE_REPORT := allure-report

.PHONY: install check check-all allure allure-live allure-serve _allure-env clean

install: ## create .venv and install the SDK + pytest
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

check: ## everything that launches nothing and bills nothing
	$(PY) -m pytest -m "not billable"

check-all: ## the full suite, including a billable sandbox
	$(PY) -m pytest

allure: ## run everything that bills nothing, into a static Allure report
	-$(PY) -m pytest -m "not billable" --alluredir=$(ALLURE_RESULTS) --clean-alluredir
	@$(MAKE) --no-print-directory _allure-env
	allure generate $(ALLURE_RESULTS) -o $(ALLURE_REPORT) --clean
	@echo "report ready: $(ALLURE_REPORT)/index.html"

allure-live: ## full live suite (LAUNCHES A SANDBOX, BILLS) into an Allure report
	-$(PY) -m pytest --alluredir=$(ALLURE_RESULTS) --clean-alluredir
	@$(MAKE) --no-print-directory _allure-env
	allure generate $(ALLURE_RESULTS) -o $(ALLURE_REPORT) --clean
	@echo "report ready: $(ALLURE_REPORT)/index.html"

allure-serve: ## build and open the report in a browser
	allure serve $(ALLURE_RESULTS)

_allure-env: ## stamp the report's Environment panel (internal)
	@$(PY) allure/stamp_env.py $(ALLURE_RESULTS)

clean:
	rm -rf $(VENV) .pytest_cache $(ALLURE_RESULTS) $(ALLURE_REPORT)
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
