.PHONY: install test dry-run run

install:
	.venv/bin/pip install -r requirements.txt

test:
	.venv/bin/python -m pytest -q

# Iterate on the advisor prompt against a fixture (no Apple Watch needed).
# Vars: FIXTURE=bad|good  RUNS=N   e.g. `make dry-run FIXTURE=good RUNS=2`
dry-run:
	.venv/bin/python scripts/dry_run.py --fixture $(or $(FIXTURE),bad) --runs $(or $(RUNS),3)

run:
	.venv/bin/uvicorn app.main:app --reload

# Prime the demo page with a known bad-night state (offline; no push/calendar).
demo-reset:
	.venv/bin/python scripts/demo_reset.py
