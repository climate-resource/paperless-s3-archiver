# Makefile to help automate key steps

.DEFAULT_GOAL := help

define PRINT_HELP_PYSCRIPT
import re, sys

for line in sys.stdin:
	match = re.match(r'^([\$$\(\)a-zA-Z_-]+):.*?## (.*)$$', line)
	if match:
		target, help = match.groups()
		print("%-30s %s" % (target, help))
endef
export PRINT_HELP_PYSCRIPT

.PHONY: help
help:  ## print short description of each target
	@python3 -c "$$PRINT_HELP_PYSCRIPT" < $(MAKEFILE_LIST)

.PHONY: checks
checks: ty licence-check  ## run all the linting checks of the codebase
	uv run pre-commit run --all-files

.PHONY: ty
ty:  ## run ty static type checks
	uv run ty check

.PHONY: ruff
ruff:  ## fix the code using ruff
    # format before and after checking so that the formatted stuff is checked and
    # the fixed stuff is formatted
	uv run ruff format src tests
	uv run ruff check src tests --fix
	uv run ruff format src tests

.PHONY: test
test:  ## run the tests
	uv run pytest tests -r a -v --cov=src

.PHONY: test-failed
test-failed:  ## re-run the failed tests
	uv run pytest tests -r a -v --last-failed --last-failed-no-failures none

.PHONY: licence-check
licence-check:  ## check that the licences of the dependencies are suitable
	uv run pylic check

.PHONY: changelog-draft
changelog-draft:  ## compile a draft of the next changelog
	uv run towncrier build --draft

.PHONY: virtual-environment
virtual-environment:  ## update the virtual environment, creating it if needed
	uv sync
	uv run pre-commit install

.PHONY: docker
docker:  ## build the container image locally
	docker build -t paperless-s3-archiver .
