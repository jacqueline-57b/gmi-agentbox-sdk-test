VENV := .venv
PY := $(VENV)/bin/python
ALLURE_RESULTS := allure-results
ALLURE_REPORT := allure-report
# One workbook, one tab per chapter; every export adds to it in place.
REPORT := docs/sdk-test-report.xlsx

.PHONY: install check check-all allure allure-live allure-serve report report-b0 report-b1 report-b2 report-b3 report-b0-export report-b1-export report-b2-export report-b3-export report-b4 report-b5 report-b4-export report-b5-export report-b6 report-b6-export _allure-env clean

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

report: report-b0-export report-b1-export report-b2-export report-b3-export report-b4-export report-b5-export report-b6-export report-b8-export ## re-export every tab from the LAST run (no live calls)

report-b0-export: ## re-export the B-0 tab from the last run (no live calls)
	$(PY) scripts/export_report.py --module tests/test_b0_agent_registration.py \
		--out $(REPORT)

report-b1-export: ## re-export the B-1 tab from the last run (no live calls)
	$(PY) scripts/export_report.py --module tests/test_b1_sandbox_startup.py \
		--out $(REPORT)

report-b2-export: ## re-export the B-2 tab from the last run (no live calls)
	$(PY) scripts/export_report.py --module tests/test_b2_command_execution.py \
		--out $(REPORT)

report-b4-export: ## re-export the B-4 tab from the last run (no live calls)
	$(PY) scripts/export_report.py --module tests/test_b4_deletion_and_expiry.py \
		--out $(REPORT)

report-b3-export: ## re-export the B-3 tab from the last run (no live calls)
	$(PY) scripts/export_report.py --module tests/test_b3_file_transfer.py \
		--out $(REPORT)

report-b5-export: ## re-export the B-5 tab from the last run (no live calls)
	$(PY) scripts/export_report.py --module tests/test_b5_retry_idempotency_quota.py \
		--out $(REPORT)

report-b6-export: ## re-export the B-6 tab from the last run (no live calls)
	$(PY) scripts/export_report.py --module tests/test_b6_tenant_isolation.py \
		--out $(REPORT)

report-b8-export: ## re-export the B-8 tab from the last run (no live calls)
	$(PY) scripts/export_report.py --module tests/test_b8_typical_workloads.py \
		--out $(REPORT)

report-b0: ## run B-0 live, then export its tab (LAUNCHES A SANDBOX, BILLS)
	$(PY) scripts/export_report.py --run tests/test_b0_agent_registration.py \
		--keep-results --out $(REPORT)

report-b1: ## run B-1 live, then export its tab (LAUNCHES 22 SANDBOXES, BILLS)
	$(PY) scripts/export_report.py --run tests/test_b1_sandbox_startup.py \
		--keep-results --out $(REPORT)

report-b2: ## run B-2 live, then export its tab (LAUNCHES SANDBOXES, BILLS)
	$(PY) scripts/export_report.py --run tests/test_b2_command_execution.py \
		--keep-results --out $(REPORT)

report-b4: ## run B-4 live, then export its tab (LAUNCHES SANDBOXES, BILLS)
	$(PY) scripts/export_report.py --run tests/test_b4_deletion_and_expiry.py \
		--keep-results --out $(REPORT)

report-b3: ## run B-3 live, then export its tab (BILLS; moves ~100MB, takes minutes)
	$(PY) scripts/export_report.py --run tests/test_b3_file_transfer.py \
		--keep-results --out $(REPORT)

report-b4: ## run B-4 live, then export its tab (LAUNCHES SANDBOXES, BILLS)
	$(PY) scripts/export_report.py --run tests/test_b4_deletion_and_expiry.py \
		--keep-results --out $(REPORT)

report-b5: ## run B-5 live, then export its tab (LAUNCHES ~35 SANDBOXES, BILLS)
	$(PY) scripts/export_report.py --run tests/test_b5_retry_idempotency_quota.py \
		--keep-results --out $(REPORT)

report-b6: ## run B-6 live, then export its tab (BILLS; needs GMI_AGENTBOX_API_KEY_2)
	$(PY) scripts/export_report.py --run tests/test_b6_tenant_isolation.py \
		--keep-results --out $(REPORT)

report-b8: ## run B-8 live, then export its tab (BILLS; ~11 sandboxes, ~40 minutes)
	$(PY) scripts/export_report.py --run tests/test_b8_typical_workloads.py \
		--keep-results --out $(REPORT)

allure-serve: ## build and open the report in a browser
	allure serve $(ALLURE_RESULTS)

_allure-env: ## stamp the report's Environment panel (internal)
	@$(PY) allure/stamp_env.py $(ALLURE_RESULTS)

clean:
	rm -rf $(VENV) .pytest_cache $(ALLURE_RESULTS) $(ALLURE_REPORT)
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
