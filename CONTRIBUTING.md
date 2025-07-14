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
   make dev
   ```

   The `make dev` command automatically installs all dependencies and sets up pre-commit hooks.

2. **Make Your Changes**:
   - Follow the existing code style and patterns
   - Add tests for new functionality
   - Update documentation if needed

3. **Test Your Changes**:
   ```bash
   # Run all tests
   make test

   # Run specific tests
   uv run pytest path/to/test_file.py
   ```

4. **Submit a Pull Request**:
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

### Makefile Commands

This project uses a Makefile for streamlined development workflow:

```bash
# Set up complete development environment (recommended for new contributors)
make dev

# Run all tests
make test

# Clean up build artifacts and cache
make clean

# View all available commands
make help
```

### Pre-commit Hooks

Pre-commit hooks are automatically installed when you run `make dev`. These hooks ensure code quality by running checks before each commit:

- **Code formatting** with `ruff format`
- **Linting** with `ruff check`
- **Type checking** with `mypy`
- **Commit message validation** for conventional commits

The hooks run automatically on `git commit` and will prevent commits that don't meet quality standards.

## Code Style

- Follow Python PEP 8 guidelines
- Use type hints for all function signatures and class attributes
- Write clear, self-documenting code
- Add docstrings for public functions and classes
- Use Pydantic models for data structures and validation

## Questions?

If you have questions about contributing, feel free to:
- Open a discussion on GitHub
- Comment on the relevant issue
- Reach out to the maintainers

We appreciate your contributions!
