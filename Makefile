.PHONY: help install dev test clean python

help: ## Show this help message
	@echo "Available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install project dependencies with full language support
	uv sync --extra treesitter-full

python: ## Install project dependencies for Python only
	uv sync

dev: ## Setup development environment (install deps + pre-commit hooks)
	uv sync --extra treesitter-full --extra dev --extra test
	uv run pre-commit install
	uv run pre-commit install --hook-type commit-msg
	@echo "✅ Development environment ready!"

test: ## Run tests
	uv run pytest

test-parallel: ## Run tests in parallel
	uv run pytest -n auto

clean: ## Clean up build artifacts and cache
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -name "*.pyc" -delete
build-grammars: ## Build grammar submodules
	git submodule update --init --recursive --depth 1
	@echo "Grammars built!"

watch: ## Watch repository for changes and update graph in real-time
	@if [ -z "$(REPO_PATH)" ]; then \
		echo "Error: REPO_PATH is required. Usage: make watch REPO_PATH=/path/to/repo"; \
		exit 1; \
	fi
	.venv/bin/python realtime_updater.py $(REPO_PATH) \
		--host $(or $(HOST),localhost) \
		--port $(or $(PORT),7687) \
		$(if $(BATCH_SIZE),--batch-size $(BATCH_SIZE),)
