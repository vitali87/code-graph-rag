# Contributing to Code Graph RAG

Thank you for your interest in contributing to Code Graph RAG! We welcome contributions from the community.

## Getting Started

1. **Browse Issues**: Check out our [GitHub Issues](https://github.com/vitali87/code-graph-rag/issues) to find tasks that need work
2. **Pick an Issue**: Choose an issue that interests you and matches your skill level
3. **Comment on the Issue**: Let us know you're working on it to avoid duplicate effort
4. **Fork the Repository**: Create your own fork to work on
5. **Create a Branch**: Use a descriptive branch name like `feat/add-feature` or `fix/bug-description`

## Development Process

1. **Set up Development Environment**:
   ```bash
   git clone https://github.com/YOUR-USERNAME/code-graph-rag.git
   cd code-graph-rag
   uv sync --extra treesitter-full --extra test --extra dev
   ```

2. **Install Pre-commit Hooks** (mandatory):
   ```bash
   pre-commit install
   ```
   All commits must pass pre-commit checks. Do not skip hooks with `--no-verify`.

3. **Make Your Changes**:
   - Follow the existing code style and patterns
   - Add tests for new functionality
   - Update documentation if needed
   - Do not add inline comments (see Comment Policy below)

4. **Test Your Changes**:
   - Run the existing tests to ensure nothing is broken
   - Test your new functionality thoroughly
   - Run `uv run ruff check` and `uv run ruff format --check` before committing

5. **Submit a Pull Request**:
   - Push your branch to your fork
   - Create a pull request against the main repository
   - Reference the issue number in your PR description
   - Provide a clear description of what you've changed and why

## Pull Request Guidelines

- Keep PRs focused on a single issue or feature
- Write clear, descriptive commit messages
- Include tests for new functionality
- Update documentation when necessary
- Be responsive to feedback during code review

## Technical Requirements

### Agentic Framework
- **PydanticAI Only**: This project uses PydanticAI as the official agentic framework. Do not introduce other frameworks like LangChain, CrewAI, or AutoGen.

### Code Standards
- **Heavy Pydantic Usage**: Use Pydantic models extensively for data validation, serialization, and configuration
- **Package Management**: Use `uv` for all dependency management and virtual environments
- **Code Quality**: Use `ruff` for linting and formatting - run `ruff check` and `ruff format` before submitting
- **Type Safety**: Use type hints everywhere and run `mypy` for type checking

### Development Tools
- **uv**: Package manager and dependency resolver
- **ruff**: Code linting and formatting (replaces flake8, black, isort)
- **mypy**: Static type checking
- **pytest**: Testing framework

### Pre-commit Hooks
This project uses `pre-commit` to automatically run checks before each commit, ensuring code quality and consistency.

To get started, first make sure you have the development dependencies installed:
```bash
uv sync --extra treesitter-full --extra test --extra dev
```
Then, install the git hooks:
```bash
pre-commit install
pre-commit autoupdate --repo https://github.com/pre-commit/pre-commit-hooks
```
Now, `pre-commit` will run automatically on `git commit`.

## Code Style

- Follow Python PEP 8 guidelines
- Use type hints for all function signatures and class attributes
- Write clear, self-documenting code
- Add docstrings for public functions and classes
- Use Pydantic models for data structures and validation

### Comment Policy

**No inline comments are allowed** unless they meet one of these criteria:

1. **Top-of-file comments**: Comments that appear before any code (including imports) are allowed
2. **`(H)` marker**: Comments containing `(H)` are allowed - this stands for "Human" and indicates an intentional, human-written comment
3. **Type annotations**: Comments containing `type:`, `noqa`, `pyright`, or `ty:` are allowed

**Why this rule exists**: AI tools (like code assistants and LLMs) tend to generate redundant, obvious comments that clutter the codebase. Comments like `# Loop through items` or `# Return the result` add no value. This policy prevents AI-generated comment slop from polluting the code.

If you need to add a comment, prefix it with `(H)`:
```python
# (H) This algorithm uses memoization because the recursive solution times out on large inputs
```

The pre-commit hook `no-inline-comments` enforces this rule automatically.

## Questions?

If you have questions about contributing, feel free to:
- Open a discussion on GitHub
- Comment on the relevant issue
- Reach out to the maintainers

We appreciate your contributions!

## Makefile Commands

This project uses a Makefile for streamlined development workflow:

```bash
# Set up complete development environment (recommended for new contributors)
make dev

# Run all tests
make test

# Run tests in parallel for faster execution
make test-parallel

# Clean up build artifacts and cache
make clean

# View all available commands
make help
```
