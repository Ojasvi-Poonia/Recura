VENV := ./.venv/bin
.PHONY: help install test seed run eval ablate sweep replay lint

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n",$$1,$$2}'

install:  ## create venv and install dependencies
	python3.13 -m venv .venv && $(VENV)/pip install -q -e ".[dev]"

test:  ## run the test suite
	$(VENV)/python -m pytest -q

seed:  ## generate the frozen synthetic cohort
	$(VENV)/python -m eval.generate_cohort

run:  ## run the agent over one episode (demo)
	$(VENV)/python -m src.api.demo

fixtures: seed  ## one-off: generate the committed LLM fixture set (needs a provider key)
	$(VENV)/python -m eval.generate_fixtures

fixtures-plan: seed  ## how many model calls the fixture set still needs
	$(VENV)/python -m eval.generate_fixtures --dry-run

validate: seed  ## benchmark validity suite: negative controls + randomisation checks
	$(VENV)/python -m eval.validate

eval: seed  ## Tier 2: batch run + metrics. Must be byte-identical across runs.
	$(VENV)/python -m eval.run_batch

ablate: seed  ## ablation study (random / no-taxonomy / no-policy / no-LLM)
	$(VENV)/python -m eval.ablate

sweep: seed  ## Tier 3: sensitivity sweep across generator parameterisations
	$(VENV)/python -m eval.sweep

replay: seed  ## policy-diff harness: replay the cohort under altered contracts
	$(VENV)/python -m eval.replay
