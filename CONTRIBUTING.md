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

### Automated Code Review

This project uses automated code review bots (**Greptile** and **Gemini Code Assist**) to provide initial feedback on PRs. Before requesting a human review:

1. **Address all bot comments**: Every comment from Greptile and Gemini Code Assist must be resolved
2. **Accept or push back**: For each bot suggestion, either:
   - **Accept**: Implement the suggestion and resolve the comment
   - **Push back**: Reply inline with a clear justification for why the suggestion doesn't apply
3. **Iterate as needed**: Continue addressing new bot comments through multiple review rounds until all are resolved
4. **Then request human review**: Only after all bot comments are cleared, assign the PR to core maintainers for human review

This process ensures that human reviewers focus on high-level design and logic rather than style and common issues that bots can catch.

## Technical Requirements

### Agentic Framework
- **PydanticAI Only**: This project uses PydanticAI as the official agentic framework. Do not introduce other frameworks like LangChain, CrewAI, or AutoGen.

### Code Standards
- **Heavy Pydantic Usage**: Use Pydantic models extensively for data validation, serialization, and configuration
- **Package Management**: Use `uv` for all dependency management and virtual environments
- **Code Quality**: Use `ruff` for linting and formatting - run `ruff check` and `ruff format` before submitting
- **Type Safety**: Use type hints everywhere and run `uv run ty check` for type checking

### Development Tools
- **uv**: Package manager and dependency resolver
- **ruff**: Code linting and formatting (replaces flake8, black, isort)
- **ty**: Static type checking (from Astral)
- **pytest**: Testing framework
- **ripgrep** (`rg`): Required for shell command text searching (install via `brew install ripgrep` on macOS or `apt install ripgrep` on Linux)

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

## Coding Standards

### Tooling

All tooling is from [Astral](https://astral.sh):

| Tool | Purpose | Command |
|------|---------|---------|
| **uv** | Package management | `uv sync`, `uv add`, `uv run` |
| **ty** | Type checking | `uv run ty check` |
| **ruff** | Linting and formatting | `uv run ruff check`, `uv run ruff format` |

```bash
# Sync dependencies
uv sync --extra dev --extra test

# Upgrade a package
uv sync --upgrade-package <pkg>

# Type check
uv run ty check codebase_rag/

# Lint and format
uv run ruff check --fix .
uv run ruff format .
```

### Type System

#### Data Structure Selection

| Structure | Use Case |
|-----------|----------|
| **StrEnum** | Constrained string constants used in comparisons, defaults, assignments |
| **NamedTuple** | Immutable records with named fields (lightweight, hashable) |
| **TypedDict** | Dict shapes for function return types or JSON-like data |
| **dataclass** | Mutable class instances with behavior/methods |
| **Pydantic BaseModel** | Configs needing validation, serialization, or schema generation |

```python
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple, TypedDict

# StrEnum - string constants
class Status(StrEnum):
    PENDING = "pending"
    DONE = "done"

# NamedTuple - immutable record
class Point(NamedTuple):
    x: float
    y: float

# TypedDict - dict shape
class Result(TypedDict):
    success: bool
    data: str

# dataclass - mutable with behavior
@dataclass
class User:
    name: str
    def greet(self) -> str:
        return f"Hello, {self.name}"
```

#### Strict Typing

- Use `Literal` types for constrained string values used only as type hints
- Use `StrEnum` when values need defaults or are used in code (not just type hints)
- Never use loose dict types like `dict[str, Any]` or `dict[str, str | int | None]` - use TypedDict instead
- Use explicit TypedDict constructors instead of plain dict literals

#### No Forward References (Quoted Type Hints)

Forward references are type hints wrapped in quotes like `"ASTNode"`. These are NOT allowed.

**How to identify forward references:**
- Type hints with quotes: `def foo(x: "SomeClass") -> "Result"`
- These appear when a type is used before it's defined or to avoid circular imports

**How to fix forward references:**
- Add `from __future__ import annotations` at the top of the file
- Remove the quotes from the type hints

**IMPORTANT:** Only add `from __future__ import annotations` to files that HAVE forward references. Do NOT add it to files that don't need it.

```python
# Bad - forward reference with quotes (THIS IS NOT ALLOWED)
def process(node: "ASTNode") -> "Result": ...

# Good - add future import and remove quotes
from __future__ import annotations

def process(node: ASTNode) -> Result: ...
```

```python
# Bad - loose dict type
def process(args: dict[str, str | int | None]) -> dict[str, Any]: ...

# Good - TypedDict with known shape
class ProcessArgs(TypedDict):
    name: str
    count: int

def process(args: ProcessArgs) -> Result: ...

# Bad - dict literal
return {"success": True, "data": data}

# Good - TypedDict constructor
return Result(success=True, data=data)
```

#### Methods Over Callable Attributes

In Protocols and mixin classes, use regular method definitions instead of `Callable` attributes. Callables are not bound (don't receive `self` implicitly) and descriptors are not invoked.

```python
from abc import abstractmethod
from typing import Callable, Protocol

# Bad - Callable attribute (not bound, not recommended)
class MyMixin:
    process: Callable[[str], int]

class MyProtocol(Protocol):
    handler: Callable[[str, int], bool]

# Good - regular method definition
# Mixin classes: use @abstractmethod for method stubs
class MyMixin:
    @abstractmethod
    def process(self, data: str) -> int: ...

# Protocols: no decorator needed (structural typing)
class MyProtocol(Protocol):
    def handler(self, name: str, count: int) -> bool: ...
```

Only use `Callable` attributes when reusing complex callable types is necessary.

### Code Organization

#### File Structure

Standard files in each module:

- `types_defs.py` - Type aliases, TypedDicts, NamedTuples (immutable structural types)
- `models.py` - Dataclasses only (runtime data structures with behavior)
- `constants.py` - StrEnums, string literals, and application constants
- `config.py` - Pydantic settings, environment config, and runtime configuration instances
- `schemas.py` - All Pydantic BaseModel classes (data transfer objects, results, responses)
- `logs.py` - Log message templates for logger calls (info, debug, warning, error, success)
- `tool_errors.py` - Error messages returned by tools to the LLM/user
- `exceptions.py` - Exception classes and their error message templates (for raise statements)

#### Modularization

- Soft rule: keep files under 700 lines (after linting); split larger files into submodules
- Group related functionality into submodules (e.g., `stem_ops/`, `tools/`, `srg_parser/`)
- Use descriptive file names that reflect purpose (e.g., `editor.py`, `factory.py`, `loader.py`)
- Each submodule can have its own `__init__.py` to expose public API

#### Import Conventions

- Import from the module's public API, not internal files:

```python
# Bad
from policy_digitization_tasks.srg_parser.editor import apply_edits

# Good
from policy_digitization_tasks.srg_parser import apply_edits
```

- Group imports: stdlib, third-party, local (separated by blank lines)
- Use explicit imports, avoid `from module import *`

#### Two-Letter Aliases for Bulk Imports

When importing 5+ items from a module, use module-level import with a 2-letter alias:

```python
# Bad - many lines of imports
from .constants import (
    CLI_ERR_CONFIG,
    CLI_MSG_DONE,
    Color,
    Provider,
    # ... 20 more items
)

# Good - 1 line with 2-letter alias
from . import constants as cs
from . import exceptions as ex
from . import tool_errors as te
from . import logs as ls

# Usage
logger.info(ls.PROCESSING_FILE.format(path=path))
raise ex.LLMGenerationError(ex.CONFIG.format(error=e))
```

#### Single Source of Truth

Define constants, patterns, and types once. Import everywhere.

#### StrEnum for Constrained Values

Use `StrEnum` when string values are used in code (defaults, comparisons, assignments):

```python
from enum import StrEnum

# Bad - hardcoded strings scattered in code
def process(mode: str = "fast"): ...
if status == "pending": ...

# Good - centralized StrEnum
class Mode(StrEnum):
    FAST = "fast"
    SLOW = "slow"

def process(mode: Mode = Mode.FAST): ...
if status == Status.PENDING: ...
```

#### Centralized Error Messages

Use an Enum with `__call__` for parameterized error messages:

```python
from enum import Enum

class Error(str, Enum):
    NOT_FOUND = "Item '{id}' not found"
    INVALID = "Invalid value"

    def __call__(self, **kwargs) -> str:
        return self.value.format(**kwargs) if kwargs else self.value

# Usage
raise ValueError(Error.NOT_FOUND(id="abc"))
```

#### Error Codes as StrEnum

Use `StrEnum` for error type names passed to exception classes:

```python
class ErrorCode(StrEnum):
    VALIDATION = "ValidationError"
    NOT_FOUND = "NotFoundError"

raise CustomError(ErrorCode.VALIDATION, Error.INVALID())
```

### Code Style

#### Loguru Over Print

Use `loguru` for all output instead of `print`:

```python
# Bad
print(f"Processing: {file}")
print(f"Error: {e}", file=sys.stderr)

# Good
from loguru import logger
logger.info(f"Processing: {file}")
logger.error(f"Error: {e}")
logger.success("Done!")
```

#### Typer Over Argparse

Use `typer` for CLI argument parsing instead of `argparse`:

```python
# Bad
parser = argparse.ArgumentParser()
parser.add_argument("name", type=str)
parser.add_argument("--count", type=int, default=1)
args = parser.parse_args()

# Good
from typing import Annotated
import typer

def main(
    name: Annotated[str, typer.Argument(help="Name")],
    count: Annotated[int, typer.Option(help="Count")] = 1,
) -> None:
    ...

typer.run(main)
```

#### Click for Nested Command Groups

Use `click` with `@click.group()` for nested subcommand groups that integrate with a typer main app:

- Typer's `add_typer()` requires more boilerplate for this pattern
- Bridge typer → click via `ctx.args` and `standalone_mode=False`
- Use `click.echo()`/`click.secho()` for user-facing CLI output (not logging)
- Add `loguru` for actual error logging in exception handlers

```python
# subcommands.py - click subcommand group
@click.group(help="Manage resources")
def cli() -> None:
    pass

@cli.command(help="Add a new resource.")
def add(name: str) -> None:
    try:
        do_add(name)
        click.echo(f"Added {name}")
    except Exception as e:
        logger.error(f"Failed to add: {e}")  # loguru for logging
        click.secho(f"Error: {e}", fg="red")  # click for user output

# main.py - typer main app bridges to click
from .subcommands import cli as subcommand_cli

@app.command(
    name="resource",
    context_settings={"allow_extra_args": True, "allow_interspersed_args": False},
)
def resource_command(ctx: typer.Context) -> None:
    subcommand_cli(ctx.args, standalone_mode=False)
```

#### No Lambdas as Dict Values

Use dataclasses with methods instead:

```python
# Bad
HANDLERS = {
    "create": lambda x: {"action": "create", "id": x.id},
}

# Good
@dataclass
class Handler:
    action: str
    template: str

    def build(self, x) -> ActionDict:
        return ActionDict(action=self.action, id=x.id)

HANDLERS = {"create": Handler(action="create", template="...")}
```

#### Match Statements Over Elif Chains

```python
# Bad
if action == "create":
    return handle_create(data)
elif action == "update":
    return handle_update(data)
else:
    return handle_default(action, data)

# Good
match action:
    case "create":
        return handle_create(data)
    case "update":
        return handle_update(data)
    case other:
        return handle_default(other, data)
```

#### Swap If-Else When If Body Is Empty

When the if body does nothing (`pass`) and all logic is in the else clause, invert the condition and remove the empty else:

```python
# Bad - empty if body with logic in else
if location == OUTSIDE:
    pass
else:
    take_off_hat()

# Good - inverted condition, no empty else
if location != OUTSIDE:
    take_off_hat()
```

This also applies when the if body is a guard condition that allows dropping the else entirely.

#### Named Expressions for Assignment + Condition

Use named expressions (`:=`) to merge assignment followed by a conditional check:

```python
# Bad - separate assignment and condition
env_base = os.environ.get("PYTHONUSERBASE", None)
if env_base:
    return env_base

chunk = file.read(8192)
while chunk:
    process(chunk)
    chunk = file.read(8192)

# Good - named expression
if env_base := os.environ.get("PYTHONUSERBASE", None):
    return env_base

while chunk := file.read(8192):
    process(chunk)
```

Named expressions can also simplify nested conditions:

```python
# Bad - nested if statements
if self._is_special:
    ans = self._check_nans(context=context)
    if ans:
        return ans

# Good - merged with named expression
if self._is_special and (ans := self._check_nans(context=context)):
    return ans
```

#### Inline Unnecessary Helpers

If a helper function is trivial and used once, inline it.

#### Move Assignments Close to Usage

Declare variables as close to their usage as possible to minimize cognitive load and prevent stranded variables:

```python
# Bad - assignment far from usage
cubes = []
function_unrelated_to_cubes()
if another_unrelated_condition():
    more_unrelated_logic()
for i in range(20):
    cubes.append(i**3)

# Good - assignment immediately before usage
function_unrelated_to_cubes()
if another_unrelated_condition():
    more_unrelated_logic()
cubes = []
for i in range(20):
    cubes.append(i**3)
```

#### DRY Reduction

Extract repeated code blocks into helpers:

```python
# Bad - same 4 lines repeated 3 times (12 lines)
def save_user(user):
    conn = db.connect()
    conn.execute(SQL_USER, user.dict())
    conn.commit()
    conn.close()

def save_order(order):
    conn = db.connect()
    conn.execute(SQL_ORDER, order.dict())
    conn.commit()
    conn.close()

def save_item(item):
    conn = db.connect()
    conn.execute(SQL_ITEM, item.dict())
    conn.commit()
    conn.close()

# Good - helper + 3 one-liners (7 lines)
def _save(sql: str, data: dict) -> None:
    conn = db.connect()
    conn.execute(sql, data)
    conn.commit()
    conn.close()

def save_user(user):
    _save(SQL_USER, user.dict())

def save_order(order):
    _save(SQL_ORDER, order.dict())

def save_item(item):
    _save(SQL_ITEM, item.dict())
```

#### No Comments or Docstrings

Code should be self-documenting. Exception: comments prefixed with `(H)` are allowed.

#### No Type Ignores, Casts, Any, or object

Never use `# type: ignore` comments, `cast()`, `Any` type, or `object` as a type hint. These provide no useful type information. Fix the underlying type issue using proper typing, type narrowing, specific union types (e.g., `str | int | bool | None`), or TypedDict for dict values.

#### No Hardcoded Strings

All repeated string literals should be constants or StrEnum members:

```python
# Bad
if node.type == "predicate_definition": ...
artifact_type = "srg_v1"

# Good
if node.type == ElementType.PREDICATE: ...
artifact_type = ARTIFACT_SRG
```

#### Almost No Strings in Code Files

Files that are NOT `config.py`, `models.py`, `constants.py`, `logs.py`, or CLI modules should have almost no string literals. Move all strings to:

- `logs.py` - all log messages (info, debug, warning, error, success)
- `constants.py` - non-log constants, StrEnums, format strings
- `tool_descriptions.py` - tool/function descriptions (for tools modules)
- `config.py` - configuration defaults

```python
# Bad - strings in service/tool files
logger.info(f"Processing file: {path}")
description="Reads file content from disk."

# Good - import from dedicated modules
from .. import logs
from . import tool_descriptions as td

logger.info(logs.PROCESSING_FILE.format(path=path))
description=td.FILE_READER
```

#### Function Signatures Use Proper Types

Use StrEnum types in function signatures, not `str`:

```python
# Bad
def extract(guideline_type: str, outcome: str = "Approve"): ...

# Good
def extract(guideline_type: GuidelineType, outcome: OutcomeType = OutcomeType.APPROVE): ...
```

### Validation

Use Pydantic `model_validator` for cross-field validation:

```python
class Config(BaseModel):
    options: list[str] | None = None
    default: str = "fallback"

    @model_validator(mode="after")
    def validate_default_in_options(self):
        if self.options and self.default not in self.options:
            raise ValueError(f"default '{self.default}' must be in options")
        return self
```

### Git Commits

- Conventional Commits format
- One-liner only
- No emoji
- No attribution or Co-Authored-By

### PR Title Convention

Uses Conventional Commits format with this regex pattern:

```
^(build|chore|ci|docs|feat|fix|perf|p?refactor|revert|style|test)(\([a-zA-Z0-9_-]+\))?!?: .+$
```

**Allowed prefixes:**

| Prefix | Purpose |
|--------|---------|
| `build` | Build system or external dependencies |
| `chore` | Routine tasks, maintenance |
| `ci` | CI configuration changes |
| `docs` | Documentation only |
| `feat` | New feature |
| `fix` | Bug fix |
| `perf` | Performance improvement |
| `refactor` or `prefactor` | Code refactoring |
| `revert` | Reverting changes |
| `style` | Formatting, whitespace, etc. |
| `test` | Adding or modifying tests |

**Format:**

```
<type>[(<scope>)][!]: <description>
```

**Examples:**

- `feat: add user authentication`
- `fix(api): resolve null pointer exception`
- `chore(deps): update dependencies`
- `feat!: breaking change to API` (the `!` indicates a breaking change)
- `refactor(core): simplify validation logic`

The scope (in parentheses) is optional and can contain alphanumeric characters, underscores, and hyphens.

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
