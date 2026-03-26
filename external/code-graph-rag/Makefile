.PHONY: help all install dev test test-parallel test-integration test-all test-parallel-all clean python build-grammars watch readme lint format typecheck check pre-commit

PYTHON := uv run

help: ## Show this help message
	@echo "Available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

all: ## Install everything for full development environment (deps, grammars, hooks, tests)
	@echo "🚀 Setting up complete development environment..."
	uv sync --all-extras
	git submodule update --init --recursive --depth 1
	$(PYTHON) pre-commit install
	$(PYTHON) pre-commit install --hook-type commit-msg
	@echo "🧪 Running tests in parallel to verify installation..."
	$(PYTHON) pytest -n auto
	@echo "✅ Full development environment ready!"
	@echo "📦 Installed: All dependencies, grammars, pre-commit hooks"
	@echo "✓ Tests passed successfully"

install: ## Install project dependencies with full language support
	uv sync --extra treesitter-full

python: ## Install project dependencies for Python only
	uv sync

dev: ## Setup development environment (install deps + pre-commit hooks)
	uv sync --extra treesitter-full --extra test --extra semantic --group dev
	$(PYTHON) pre-commit install
	$(PYTHON) pre-commit install --hook-type commit-msg
	@echo "✅ Development environment ready!"

test: ## Run unit tests only (fast, no Docker)
	$(PYTHON) pytest -m "not integration"

test-parallel: ## Run unit tests in parallel (fast, no Docker)
	$(PYTHON) pytest -n auto -m "not integration"

test-integration: ## Run integration tests (requires Docker)
	$(PYTHON) pytest -m "integration" -v

test-all: ## Run all tests including integration and e2e (requires Docker)
	$(PYTHON) pytest -v

test-parallel-all: ## Run all tests in parallel including integration and e2e (requires Docker)
	$(PYTHON) pytest -n auto

clean: ## Clean up build artifacts and cache
	rm -rf .pytest_cache/ .ty/ .ruff_cache/
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

readme: ## Regenerate README.md from codebase
	$(PYTHON) python -X utf8 scripts/generate_readme.py

lint: ## Run ruff check
	$(PYTHON) ruff check .

format: ## Run ruff format
	$(PYTHON) ruff format .

typecheck: ## Run type checking with ty
	$(PYTHON) ty check -v --exclude codebase_rag/tests/

check: lint typecheck test ## Run all checks: lint, typecheck, test

pre-commit: ## Run all pre-commit checks locally (comprehensive test before commit)
	@echo "Running pre-commit checks..."
	@echo "1. Formatting code..."
	$(PYTHON) ruff format .
	@echo "2. Linting and fixing..."
	$(PYTHON) ruff check --fix .
	@echo "3. Type checking..."
	$(PYTHON) ty check --exclude codebase_rag/tests/
	@echo "4. Checking for missing docstrings..."
	$(PYTHON) python scripts/check_no_docs.py
	@echo "5. Generating README sections..."
	$(PYTHON) python scripts/hooks/generate_readme.py
	@echo "6. Running security checks..."
	$(PYTHON) bandit -c pyproject.toml --severity-level high -r codebase_rag/ --exclude codebase_rag/tests/,scripts/
	@echo "7. Running unit tests (integration tests skipped - run 'make test-integration' separately)..."
	$(PYTHON) pytest -n auto -m "not integration"
	@echo "All pre-commit checks passed!"
