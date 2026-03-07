from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from tree_sitter import Node

from .. import constants as cs


@dataclass(frozen=True)
class HTTPCallSite:
    caller_qualified_name: str
    http_method: str
    url_pattern: str
    library: str
    line_number: int
    file_path: str


def detect_http_calls_in_source(
    file_path: Path,
    root_node: Node,
    language: cs.SupportedLanguage,
    module_qn: str,
) -> list[HTTPCallSite]:
    lang_key = language.value
    patterns = cs.HTTP_CLIENT_PATTERNS.get(lang_key)
    if not patterns:
        return []

    source = root_node.text
    if source is None:
        return []

    source_text = source.decode(cs.ENCODING_UTF8)
    calls: list[HTTPCallSite] = []

    modules = patterns["modules"]
    methods = patterns["methods"]

    for mod in modules:
        for method in methods:
            found = _find_http_calls_in_text(
                source_text, mod, method, module_qn, str(file_path)
            )
            calls.extend(found)

    if calls:
        logger.debug(
            "Detected {} HTTP call(s) in {}",
            len(calls),
            file_path,
        )

    return calls


def _find_http_calls_in_text(
    source_text: str,
    module_name: str,
    method_name: str,
    module_qn: str,
    file_path: str,
) -> list[HTTPCallSite]:
    results: list[HTTPCallSite] = []

    escaped_mod = re.escape(module_name)
    escaped_method = re.escape(method_name)

    # Match patterns like: requests.get("url"), httpx.post(url), axios.get(url)
    # Also match: client.get("url") where client was imported from the module
    # Allow optional TypeScript generics between method name and opening paren
    pattern = re.compile(
        rf"(?:{escaped_mod}|[a-zA-Z_]\w*)\.{escaped_method}\s*(?:<[^>]*>)?\s*\("
        rf"([^)]*)\)",
        re.MULTILINE,
    )

    # Extract all quoted strings from an argument list
    _quoted_re = re.compile(r"""["']([^"']*)["']""")

    # HTTP method names that should be skipped when looking for URLs
    _http_methods = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

    lines = source_text.split("\n")
    for line_num, line in enumerate(lines, start=1):
        for match in pattern.finditer(line):
            args_text = match.group(1) or ""
            # Find the first quoted string that looks like a URL (not an HTTP method)
            url = ""
            for qm in _quoted_re.finditer(args_text):
                candidate = qm.group(1)
                if candidate.upper() not in _http_methods:
                    url = candidate
                    break
            url_path = _extract_url_path(url)
            http_method = _normalize_http_method(method_name)

            results.append(
                HTTPCallSite(
                    caller_qualified_name=module_qn,
                    http_method=http_method,
                    url_pattern=url_path,
                    library=module_name,
                    line_number=line_num,
                    file_path=file_path,
                )
            )

    return results


def _extract_url_path(url: str) -> str:
    if not url:
        return ""

    # Strip protocol and host to get just the path
    url = re.sub(r"https?://[^/]*", "", url)

    # Normalize path parameter patterns
    # /users/{id} or /users/:id or /users/<id> -> /users/{id}
    url = re.sub(r":(\w+)", r"{\1}", url)
    url = re.sub(r"<(\w+)>", r"{\1}", url)

    # Handle f-string interpolation: /users/{user_id} stays as-is
    # Handle string concatenation artifacts
    url = url.strip("/")
    if url:
        url = "/" + url

    return url or "/"


def _normalize_http_method(method_name: str) -> str:
    method_map = {
        "get": "GET",
        "post": "POST",
        "put": "PUT",
        "delete": "DELETE",
        "patch": "PATCH",
        "head": "HEAD",
        "options": "OPTIONS",
        "request": "UNKNOWN",
        "send": "UNKNOWN",
        "fetch": "UNKNOWN",
        "do": "UNKNOWN",
        "execute": "UNKNOWN",
        "exchange": "UNKNOWN",
        "newrequest": "UNKNOWN",
        "newcall": "UNKNOWN",
        "getforobject": "GET",
        "postforobject": "POST",
    }
    return method_map.get(method_name.lower(), "UNKNOWN")
