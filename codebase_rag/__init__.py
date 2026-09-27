"""Code Graph RAG: build a queryable graph of a codebase and reason over it."""

import os

# pydantic-ai depends on logfire, whose pydantic plugin pydantic loads the first
# time any model or settings class is built. That imports OpenTelemetry and
# friends (~0.13 s) on every `cgr` start, and cgr never uses logfire (issue
# #2253). A value the user has set, including an empty one that keeps every
# plugin, is left alone.
os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "logfire-plugin")
