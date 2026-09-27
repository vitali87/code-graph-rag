"""Process defaults for the `cgr` command, applied before any settings load.

`codebase_rag.cli` imports this first among its own modules. pydantic-ai
depends on logfire, whose pydantic plugin pydantic loads the first time a
model or settings class is built; that imports OpenTelemetry and friends
(~0.13 s) on every start, and cgr never uses logfire (issue #2253). The
default applies to the CLI process only, so an application embedding
codebase_rag keeps its own plugins. A value the user set, including an empty
one that keeps every plugin, is left alone.
"""

import os

os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "logfire-plugin")
