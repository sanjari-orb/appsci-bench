# Convenience wrapper around the generic human-baseline template (setup_standalone.py, dev_deploy.py,
# elicit.py, checkpoint.py, score_final.py). See README.md for the full explanation of each step;
# this Makefile just gives each one a short name so you don't retype --task/--artifact-dir every time.
#
# Usage:
#   make attempt TASK=order_to_cash LEVEL=L3 BASELINER=P5   # creates the attempt
#   make standalone                                             # then run each step -- no export
#   make up                                                     # needed: ATTEMPT defaults to the
#   make dev-deploy                                             # attempt `make attempt` just created
#   ...
#   make ATTEMPT=<other_id> up   # override explicitly to act on a DIFFERENT attempt instead
#
# `make attempt` records the id it created in .last_attempt (gitignored, local to this dir); ATTEMPT
# defaults to that file's contents when not passed explicitly. Once ATTEMPT is resolved,
# TASK_ID/STANDALONE_DIR/ARTIFACT_DIR are read out of that attempt's own config.yaml (single source
# of truth -- same file new_attempt.sh writes), so you never repeat them on the command line either.
.PHONY: help attempt standalone up author dev-deploy dev-deploy-stop elicit checkpoint final-score down clean

TEMPLATE_DIR := $(shell cd "$(dir $(abspath $(lastword $(MAKEFILE_LIST))))" && pwd)
ATTEMPTS_DIR := $(TEMPLATE_DIR)/../attempts
REPO_ROOT    := $(TEMPLATE_DIR)/../..
LAST_ATTEMPT_FILE := $(TEMPLATE_DIR)/.last_attempt

# Prefer the repo's own venv (has uvicorn/appsci_bench installed); fall back to plain python3.
PYTHON := $(shell test -x "$(REPO_ROOT)/.venv/bin/python" && echo "$(REPO_ROOT)/.venv/bin/python" || echo python3)

# Explicit ATTEMPT=... on the command line wins; otherwise fall back to the last one `make attempt`
# created. Both stay empty (not an error) until something sets them, so `make attempt`/`make help`
# work with nothing set yet.
ATTEMPT ?= $(shell cat "$(LAST_ATTEMPT_FILE)" 2>/dev/null)
ATTEMPT_DIR := $(ATTEMPTS_DIR)/$(ATTEMPT)
CONFIG      := $(ATTEMPT_DIR)/config.yaml

# Read a field out of the attempt's config.yaml. Only evaluated by targets that need it (via
# recursive $(call), not eagerly), so `make attempt` and `make help` work with no ATTEMPT set.
define read_config
$(shell $(PYTHON) -c "import yaml,sys; print(yaml.safe_load(open('$(CONFIG)'))$(1) or '')" 2>/dev/null)
endef

# Params (checkpoint accepts extra ones on the command line, e.g. `make checkpoint MARK=2h LIMIT=10`).
LEVEL      ?= L3
BASELINER  ?= human
MARK       ?= 1h
LIMIT      ?= 5
PERSONA    ?= l1

define require_attempt
if [ -z "$(ATTEMPT)" ]; then echo "ERROR: no attempt selected -- run 'make attempt' first, or pass ATTEMPT=<attempt_id> (see ls $(ATTEMPTS_DIR))" >&2; exit 1; fi; \
if [ ! -f "$(CONFIG)" ]; then echo "ERROR: no config.yaml for ATTEMPT=$(ATTEMPT) (looked in $(CONFIG)) -- check the id, or ls $(ATTEMPTS_DIR)" >&2; exit 1; fi
endef

help:
	@echo "Generic human-baseline template -- Makefile shortcuts. See README.md for full detail."
	@echo
	@echo "  make attempt TASK=<task_id> LEVEL=<L1|L2|L3> BASELINER=<name>"
	@echo "      Create a new attempt (human_baseline/attempts/<attempt_id>/). Every target below then"
	@echo "      defaults to it automatically -- pass ATTEMPT=<id> explicitly to act on a different one."
	@echo
	@echo "  make standalone"
	@echo "      Build the standalone workspace (artifact/ scaffold, sop/) and record its path"
	@echo "      in the attempt's config.yaml."
	@echo
	@echo "  make up             bring the task-env up (DB, tool API, ... -- same as a real agent gets)"
	@echo "  make author         print where to open Claude Code / your editor"
	@echo "  make dev-deploy      build+run your CURRENT artifact/ against the live task-env (edit/test loop)"
	@echo "  make dev-deploy-stop stop the dev-deploy container"
	@echo "  make elicit PERSONA=<l1|l2|l3>   chat with the task's user-sim persona"
	@echo "  make checkpoint MARK=<1h|2h|3h30|end> LIMIT=<n>   real score against a few samples"
	@echo "  make final-score     score the FULL held-out eval set (the number that counts)"
	@echo "  make down             tear down the task-env (and the dev-deploy container)"
	@echo
	@echo "  make clean            alias for 'down' (containers only -- never deletes files)"
	@if [ -n "$(ATTEMPT)" ]; then echo; echo "Current ATTEMPT: $(ATTEMPT)"; fi

attempt:
	@if [ -z "$(TASK)" ] || [ -z "$(BASELINER)" ]; then \
	  echo "Usage: make attempt TASK=<task_id> LEVEL=<L1|L2|L3> BASELINER=<name>" >&2; exit 1; \
	fi
	@out="$$("$(TEMPLATE_DIR)/new_attempt.sh" "$(TASK)" "$(LEVEL)" "$(BASELINER)")" || { echo "$$out" >&2; exit 1; }; \
	id="$$(echo "$$out" | sed -n 's/^=== Created attempt: .*\/\([^/]*\) ===$$/\1/p')"; \
	echo "$$id" > "$(LAST_ATTEMPT_FILE)"; \
	echo "attempt_id=$$id"

standalone:
	@$(require_attempt)
	@$(PYTHON) "$(TEMPLATE_DIR)/setup_standalone.py" --task $(call read_config,['task_id']) \
	    --dir "$$HOME/Documents/baseline-work/$(ATTEMPT)"
	@$(PYTHON) -c "\
import yaml; \
p = '$(CONFIG)'; \
d = yaml.safe_load(open(p)); \
d['standalone_dir'] = '$$HOME/Documents/baseline-work/$(ATTEMPT)'; \
yaml.safe_dump(d, open(p, 'w'), sort_keys=False)"

up:
	@$(require_attempt)
	cd "$(REPO_ROOT)" && $(PYTHON) -m appsci_bench.orchestrator.eval_artifact up --phase build --task $(call read_config,['task_id'])

author:
	@$(require_attempt)
	@echo "Open Claude Code / your editor rooted at: $(call read_config,['standalone_dir'])"
	@echo "Author artifact/Dockerfile + artifact/server.py (see tasks/$(call read_config,['task_id'])/spec.yaml's interface_design)."

dev-deploy:
	@$(require_attempt)
	cd "$(REPO_ROOT)" && $(PYTHON) "$(TEMPLATE_DIR)/dev_deploy.py" --task $(call read_config,['task_id']) \
	    --artifact-dir "$(call read_config,['standalone_dir'])/artifact"

dev-deploy-stop:
	@$(require_attempt)
	cd "$(REPO_ROOT)" && $(PYTHON) "$(TEMPLATE_DIR)/dev_deploy.py" --task $(call read_config,['task_id']) \
	    --artifact-dir "$(call read_config,['standalone_dir'])/artifact" --stop

elicit:
	@$(require_attempt)
	cd "$(REPO_ROOT)" && $(PYTHON) "$(TEMPLATE_DIR)/elicit.py" --task $(call read_config,['task_id']) \
	    --persona $(PERSONA) --attempt $(ATTEMPT)

checkpoint:
	@$(require_attempt)
	cd "$(REPO_ROOT)" && $(PYTHON) "$(TEMPLATE_DIR)/checkpoint.py" --task $(call read_config,['task_id']) \
	    --attempt $(ATTEMPT) --artifact-dir "$(call read_config,['standalone_dir'])/artifact" \
	    --mark $(MARK) --limit $(LIMIT) --attach

final-score:
	@$(require_attempt)
	cd "$(REPO_ROOT)" && $(PYTHON) "$(TEMPLATE_DIR)/score_final.py" --task $(call read_config,['task_id']) \
	    --attempt $(ATTEMPT) --artifact-dir "$(call read_config,['standalone_dir'])/artifact"

down:
	@$(require_attempt)
	cd "$(REPO_ROOT)" && $(PYTHON) -m appsci_bench.orchestrator.eval_artifact down --task $(call read_config,['task_id']) || true
	cd "$(REPO_ROOT)" && $(PYTHON) "$(TEMPLATE_DIR)/dev_deploy.py" --task $(call read_config,['task_id']) \
	    --artifact-dir "$(call read_config,['standalone_dir'])/artifact" --stop || true

clean: down
