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

clean: ## Clean up build artifacts and cache
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -name "*.pyc" -delete
build-grammars:
	git submodule update --init --recursive
	@echo "Grammars built!"
