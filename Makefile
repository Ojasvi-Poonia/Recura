# Overridable so the container can use its system interpreter.
VENV ?= ./.venv/bin
.PHONY: help install test seed run eval ablate sweep replay lint

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n",$$1,$$2}'

install:  ## create venv and install dependencies
	python3.13 -m venv .venv && $(VENV)/pip install -q -e ".[api,llm,dev]"

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

voice:  ## render Hinglish recovery voice samples (demo only)
	$(VENV)/python -m src.act.voice

calibration: seed  ## is the model's stated confidence trustworthy?
	$(VENV)/python -m eval.calibration

validate: seed  ## benchmark validity suite: negative controls + randomisation checks
	$(VENV)/python -m eval.validate

# LIVE=1 streams every decision; PACE and LIMIT make it filmable.
#   make eval                       the table
#   make eval LIVE=1                the table, with the decisions that produced it
#   make eval LIVE=1 PACE=0.06      slow enough to record
LIVE_ARGS = $(if $(LIVE),--live)$(if $(PACE), --pace $(PACE))$(if $(LIMIT), --limit $(LIMIT))

eval: seed  ## Tier 2: batch run + metrics. Must be byte-identical across runs.
	$(VENV)/python -m eval.run_batch $(LIVE_ARGS)

ablate: seed  ## ablation study (random / no-taxonomy / no-policy / no-LLM)
	$(VENV)/python -m eval.ablate

sweep: seed  ## Tier 3: sensitivity sweep across generator parameterisations
	$(VENV)/python -m eval.sweep

replay: seed  ## policy-diff harness: replay the cohort under altered contracts
	$(VENV)/python -m eval.replay
