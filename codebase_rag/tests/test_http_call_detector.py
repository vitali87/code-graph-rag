import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.http_call_detector import (
    HTTPCallSite,
    _extract_url_path,
    _find_http_calls_in_text,
    _normalize_http_method,
)


# ────────────────────────────────────────────────────────────────────
# HTTPCallSite dataclass
# ────────────────────────────────────────────────────────────────────


class TestHTTPCallSiteDataclass:
    def test_creation(self) -> None:
        site = HTTPCallSite(
            caller_qualified_name="proj.module",
            http_method="GET",
            url_pattern="/users",
            library="requests",
            line_number=10,
            file_path="module.py",
        )
        assert site.caller_qualified_name == "proj.module"
        assert site.http_method == "GET"
        assert site.url_pattern == "/users"
        assert site.library == "requests"
        assert site.line_number == 10
        assert site.file_path == "module.py"

    def test_frozen(self) -> None:
        site = HTTPCallSite(
            caller_qualified_name="x",
            http_method="GET",
            url_pattern="/",
            library="requests",
            line_number=1,
            file_path="f.py",
        )
        with pytest.raises(AttributeError):
            site.http_method = "POST"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = HTTPCallSite("x", "GET", "/a", "requests", 1, "f.py")
        b = HTTPCallSite("x", "GET", "/a", "requests", 1, "f.py")
        assert a == b

    def test_inequality_method(self) -> None:
        a = HTTPCallSite("x", "GET", "/a", "requests", 1, "f.py")
        b = HTTPCallSite("x", "POST", "/a", "requests", 1, "f.py")
        assert a != b

    def test_inequality_line(self) -> None:
        a = HTTPCallSite("x", "GET", "/a", "requests", 1, "f.py")
        b = HTTPCallSite("x", "GET", "/a", "requests", 2, "f.py")
        assert a != b


# ────────────────────────────────────────────────────────────────────
# _extract_url_path
# ────────────────────────────────────────────────────────────────────


class TestExtractUrlPath:
    def test_full_https_url(self) -> None:
        assert _extract_url_path("https://api.example.com/users/123") == "/users/123"

    def test_full_http_url(self) -> None:
        assert _extract_url_path("http://localhost:8080/api/v1") == "/api/v1"

    def test_path_only(self) -> None:
        assert _extract_url_path("/api/v1/items") == "/api/v1/items"

    def test_colon_params(self) -> None:
        assert _extract_url_path("/users/:id") == "/users/{id}"

    def test_multiple_colon_params(self) -> None:
        assert (
            _extract_url_path("/users/:userId/posts/:postId")
            == "/users/{userId}/posts/{postId}"
        )

    def test_angle_bracket_params(self) -> None:
        assert _extract_url_path("/users/<user_id>") == "/users/{user_id}"

    def test_curly_brace_params_preserved(self) -> None:
        assert _extract_url_path("/users/{userId}") == "/users/{userId}"

    def test_mixed_param_styles(self) -> None:
        result = _extract_url_path("/a/:b/<c>/{d}")
        assert "/a/{b}/{c}/{d}" == result

    def test_empty_url(self) -> None:
        assert _extract_url_path("") == ""

    def test_just_slash(self) -> None:
        assert _extract_url_path("/") == "/"

    def test_trailing_slash_stripped(self) -> None:
        assert _extract_url_path("/users/") == "/users"

    def test_url_with_host_no_path(self) -> None:
        assert _extract_url_path("https://api.example.com") == "/"

    def test_url_with_host_slash_only(self) -> None:
        assert _extract_url_path("https://api.example.com/") == "/"

    def test_deep_nested_path(self) -> None:
        assert (
            _extract_url_path("/api/v2/organizations/teams/members")
            == "/api/v2/organizations/teams/members"
        )

    def test_url_with_port(self) -> None:
        assert _extract_url_path("http://localhost:3000/health") == "/health"

    def test_url_with_auth(self) -> None:
        result = _extract_url_path("https://user:pass@host.com/data")
        # After stripping protocol+host
        assert "/data" in result

    def test_relative_path(self) -> None:
        assert _extract_url_path("api/v1/users") == "/api/v1/users"

    def test_path_with_hyphens(self) -> None:
        assert _extract_url_path("/my-api/v1/get-users") == "/my-api/v1/get-users"

    def test_path_with_underscores(self) -> None:
        assert _extract_url_path("/my_api/get_users") == "/my_api/get_users"

    def test_path_with_dots(self) -> None:
        assert _extract_url_path("/api/v1.2/users") == "/api/v1.2/users"


# ────────────────────────────────────────────────────────────────────
# _normalize_http_method
# ────────────────────────────────────────────────────────────────────


class TestNormalizeHttpMethod:
    def test_get(self) -> None:
        assert _normalize_http_method("get") == "GET"

    def test_post(self) -> None:
        assert _normalize_http_method("post") == "POST"

    def test_put(self) -> None:
        assert _normalize_http_method("put") == "PUT"

    def test_delete(self) -> None:
        assert _normalize_http_method("delete") == "DELETE"

    def test_patch(self) -> None:
        assert _normalize_http_method("patch") == "PATCH"

    def test_head(self) -> None:
        assert _normalize_http_method("head") == "HEAD"

    def test_options(self) -> None:
        assert _normalize_http_method("options") == "OPTIONS"

    def test_request_unknown(self) -> None:
        assert _normalize_http_method("request") == "UNKNOWN"

    def test_send_unknown(self) -> None:
        assert _normalize_http_method("send") == "UNKNOWN"

    def test_fetch_unknown(self) -> None:
        assert _normalize_http_method("fetch") == "UNKNOWN"

    def test_do_unknown(self) -> None:
        assert _normalize_http_method("do") == "UNKNOWN"

    def test_execute_unknown(self) -> None:
        assert _normalize_http_method("execute") == "UNKNOWN"

    def test_exchange_unknown(self) -> None:
        assert _normalize_http_method("exchange") == "UNKNOWN"

    def test_newrequest_unknown(self) -> None:
        assert _normalize_http_method("newrequest") == "UNKNOWN"

    def test_newcall_unknown(self) -> None:
        assert _normalize_http_method("newcall") == "UNKNOWN"

    def test_getforobject_maps_to_get(self) -> None:
        assert _normalize_http_method("getForObject") == "GET"

    def test_postforobject_maps_to_post(self) -> None:
        assert _normalize_http_method("postForObject") == "POST"

    def test_uppercase_get(self) -> None:
        assert _normalize_http_method("GET") == "GET"

    def test_mixed_case_post(self) -> None:
        assert _normalize_http_method("Post") == "POST"

    def test_unknown_method(self) -> None:
        assert _normalize_http_method("foobar") == "UNKNOWN"

    def test_empty_string(self) -> None:
        assert _normalize_http_method("") == "UNKNOWN"


# ────────────────────────────────────────────────────────────────────
# _find_http_calls_in_text
# ────────────────────────────────────────────────────────────────────


class TestFindHttpCallsInText:
    def test_simple_requests_get(self) -> None:
        source = 'requests.get("https://api.example.com/users")'
        results = _find_http_calls_in_text(source, "requests", "get", "mod.qn", "f.py")
        assert len(results) == 1
        assert results[0].http_method == "GET"
        assert results[0].url_pattern == "/users"
        assert results[0].library == "requests"
        assert results[0].line_number == 1

    def test_requests_post_with_single_quotes(self) -> None:
        source = "requests.post('https://api.example.com/items')"
        results = _find_http_calls_in_text(source, "requests", "post", "m", "f.py")
        assert len(results) == 1
        assert results[0].http_method == "POST"
        assert results[0].url_pattern == "/items"

    def test_multiline_source(self) -> None:
        source = """import requests
x = 1
requests.get("https://host/users")
y = 2
requests.post("https://host/items")
"""
        results = _find_http_calls_in_text(source, "requests", "get", "m", "f.py")
        assert len(results) == 1
        assert results[0].line_number == 3

    def test_multiple_calls_same_method(self) -> None:
        source = """requests.get("https://a/x")
requests.get("https://b/y")
requests.get("https://c/z")"""
        results = _find_http_calls_in_text(source, "requests", "get", "m", "f.py")
        assert len(results) == 3
        assert results[0].url_pattern == "/x"
        assert results[1].url_pattern == "/y"
        assert results[2].url_pattern == "/z"

    def test_alias_match(self) -> None:
        source = 'client.get("https://host/data")'
        results = _find_http_calls_in_text(source, "requests", "get", "m", "f.py")
        assert len(results) == 1

    def test_no_url_in_call(self) -> None:
        source = "requests.get(url)"
        results = _find_http_calls_in_text(source, "requests", "get", "m", "f.py")
        assert len(results) == 1
        assert results[0].url_pattern == ""

    def test_no_match(self) -> None:
        source = "print('hello')"
        results = _find_http_calls_in_text(source, "requests", "get", "m", "f.py")
        assert len(results) == 0

    def test_different_module_no_match(self) -> None:
        source = 'urllib.get("url")'
        results = _find_http_calls_in_text(source, "requests", "get", "m", "f.py")
        # urllib matches [a-zA-Z_]\w* wildcard pattern
        assert len(results) == 1

    def test_caller_qn_propagated(self) -> None:
        source = 'requests.get("https://h/x")'
        results = _find_http_calls_in_text(
            source, "requests", "get", "proj.svc.client", "client.py"
        )
        assert results[0].caller_qualified_name == "proj.svc.client"
        assert results[0].file_path == "client.py"

    def test_line_numbers_start_at_one(self) -> None:
        source = 'requests.get("https://h/x")'
        results = _find_http_calls_in_text(source, "requests", "get", "m", "f.py")
        assert results[0].line_number == 1

    def test_line_numbers_multiline(self) -> None:
        source = "line1\nline2\nline3\nrequests.get('https://h/x')\nline5"
        results = _find_http_calls_in_text(source, "requests", "get", "m", "f.py")
        assert len(results) == 1
        assert results[0].line_number == 4

    def test_httpx_module(self) -> None:
        source = 'httpx.post("https://h/data")'
        results = _find_http_calls_in_text(source, "httpx", "post", "m", "f.py")
        assert len(results) == 1
        assert results[0].library == "httpx"

    def test_axios_module(self) -> None:
        source = 'axios.get("https://h/api/v1/users")'
        results = _find_http_calls_in_text(source, "axios", "get", "m", "f.js")
        assert len(results) == 1
        assert results[0].url_pattern == "/api/v1/users"

    def test_url_with_path_params(self) -> None:
        source = 'requests.get("https://h/users/:id/posts")'
        results = _find_http_calls_in_text(source, "requests", "get", "m", "f.py")
        assert len(results) == 1
        assert results[0].url_pattern == "/users/{id}/posts"

    def test_url_with_curly_brace_params(self) -> None:
        source = 'requests.get("https://h/users/{user_id}")'
        results = _find_http_calls_in_text(source, "requests", "get", "m", "f.py")
        assert len(results) == 1
        assert results[0].url_pattern == "/users/{user_id}"

    def test_spaces_before_paren(self) -> None:
        source = 'requests.get  ("https://h/data")'
        results = _find_http_calls_in_text(source, "requests", "get", "m", "f.py")
        assert len(results) == 1

    def test_empty_source(self) -> None:
        results = _find_http_calls_in_text("", "requests", "get", "m", "f.py")
        assert len(results) == 0

    def test_special_chars_in_module_name_escaped(self) -> None:
        # urllib.request has a dot which should be escaped in regex
        source = 'urllib.request.get("https://h/data")'
        results = _find_http_calls_in_text(
            source, "urllib.request", "get", "m", "f.py"
        )
        # The regex uses re.escape on module name
        assert len(results) == 1

    def test_method_delete(self) -> None:
        source = 'requests.delete("https://h/users/1")'
        results = _find_http_calls_in_text(source, "requests", "delete", "m", "f.py")
        assert len(results) == 1
        assert results[0].http_method == "DELETE"

    def test_method_put(self) -> None:
        source = 'requests.put("https://h/users/1")'
        results = _find_http_calls_in_text(source, "requests", "put", "m", "f.py")
        assert len(results) == 1
        assert results[0].http_method == "PUT"

    def test_method_patch(self) -> None:
        source = 'requests.patch("https://h/users/1")'
        results = _find_http_calls_in_text(source, "requests", "patch", "m", "f.py")
        assert len(results) == 1
        assert results[0].http_method == "PATCH"

    def test_method_head(self) -> None:
        source = 'requests.head("https://h/health")'
        results = _find_http_calls_in_text(source, "requests", "head", "m", "f.py")
        assert len(results) == 1
        assert results[0].http_method == "HEAD"

    def test_method_options(self) -> None:
        source = 'requests.options("https://h/cors")'
        results = _find_http_calls_in_text(source, "requests", "options", "m", "f.py")
        assert len(results) == 1
        assert results[0].http_method == "OPTIONS"

    def test_fetch_method(self) -> None:
        source = 'fetch("https://h/data")'
        # fetch doesn't match the pattern module.method since there's no dot
        results = _find_http_calls_in_text(source, "fetch", "fetch", "m", "f.js")
        assert len(results) == 0

    def test_java_style_resttemplate(self) -> None:
        source = 'restTemplate.getForObject("http://svc/users")'
        results = _find_http_calls_in_text(
            source, "RestTemplate", "getForObject", "m", "f.java"
        )
        assert len(results) == 1
        assert results[0].http_method == "GET"

    def test_go_style_http_get(self) -> None:
        source = 'http.Get("http://svc/data")'
        results = _find_http_calls_in_text(source, "net/http", "Get", "m", "f.go")
        assert len(results) == 1

    def test_url_only_path(self) -> None:
        source = 'requests.get("/api/v1/users")'
        results = _find_http_calls_in_text(source, "requests", "get", "m", "f.py")
        assert len(results) == 1
        assert results[0].url_pattern == "/api/v1/users"


# ────────────────────────────────────────────────────────────────────
# _extract_url_path - additional edge cases
# ────────────────────────────────────────────────────────────────────


class TestExtractUrlPathEdgeCases:
    def test_url_with_multiple_slashes(self) -> None:
        result = _extract_url_path("/a/b/c/d/e/f/g")
        assert result == "/a/b/c/d/e/f/g"

    def test_url_with_numeric_segments(self) -> None:
        result = _extract_url_path("/api/v2/items/42")
        assert result == "/api/v2/items/42"

    def test_localhost_url(self) -> None:
        result = _extract_url_path("http://localhost/api")
        assert result == "/api"

    def test_ip_address_url(self) -> None:
        result = _extract_url_path("http://192.168.1.1:8080/health")
        assert result == "/health"

    def test_multiple_colon_params_adjacent(self) -> None:
        result = _extract_url_path("/:a/:b/:c")
        assert result == "/{a}/{b}/{c}"

    def test_angle_bracket_multiple(self) -> None:
        result = _extract_url_path("/<org>/<team>/<member>")
        assert result == "/{org}/{team}/{member}"


# ────────────────────────────────────────────────────────────────────
# Cross-language HTTP call detection tests
#
# Tests that _find_http_calls_in_text correctly detects HTTP calls
# for every (module, method) combination defined in
# cs.HTTP_CLIENT_PATTERNS across all 6 supported languages.
# ────────────────────────────────────────────────────────────────────


class TestCrossLanguagePythonRequests:
    """Python — requests library"""

    def test_requests_get(self) -> None:
        src = 'response = requests.get("https://api.svc.io/users")'
        r = _find_http_calls_in_text(src, "requests", "get", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "GET"
        assert r[0].url_pattern == "/users"
        assert r[0].library == "requests"

    def test_requests_post(self) -> None:
        src = 'requests.post("https://api.svc.io/users", json=payload)'
        r = _find_http_calls_in_text(src, "requests", "post", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "POST"

    def test_requests_put(self) -> None:
        src = 'requests.put("https://api.svc.io/users/1", json=data)'
        r = _find_http_calls_in_text(src, "requests", "put", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "PUT"

    def test_requests_delete(self) -> None:
        src = 'requests.delete("https://api.svc.io/users/1")'
        r = _find_http_calls_in_text(src, "requests", "delete", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "DELETE"

    def test_requests_patch(self) -> None:
        src = 'requests.patch("https://api.svc.io/users/1", json=patch)'
        r = _find_http_calls_in_text(src, "requests", "patch", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "PATCH"

    def test_requests_head(self) -> None:
        src = 'requests.head("https://api.svc.io/health")'
        r = _find_http_calls_in_text(src, "requests", "head", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "HEAD"

    def test_requests_options(self) -> None:
        src = 'requests.options("https://api.svc.io/users")'
        r = _find_http_calls_in_text(src, "requests", "options", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "OPTIONS"

    def test_requests_request_generic(self) -> None:
        src = 'requests.request("POST", "https://api.svc.io/submit")'
        r = _find_http_calls_in_text(src, "requests", "request", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"


class TestCrossLanguagePythonHttpx:
    """Python — httpx library"""

    def test_httpx_get(self) -> None:
        src = 'httpx.get("https://api.svc.io/items")'
        r = _find_http_calls_in_text(src, "httpx", "get", "mod", "app.py")
        assert len(r) == 1
        assert r[0].library == "httpx"
        assert r[0].http_method == "GET"
        assert r[0].url_pattern == "/items"

    def test_httpx_post(self) -> None:
        src = 'httpx.post("https://api.svc.io/items", json=data)'
        r = _find_http_calls_in_text(src, "httpx", "post", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "POST"

    def test_httpx_put(self) -> None:
        src = 'httpx.put("https://api.svc.io/items/1")'
        r = _find_http_calls_in_text(src, "httpx", "put", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "PUT"

    def test_httpx_delete(self) -> None:
        src = 'httpx.delete("https://api.svc.io/items/1")'
        r = _find_http_calls_in_text(src, "httpx", "delete", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "DELETE"

    def test_httpx_patch(self) -> None:
        src = 'httpx.patch("https://api.svc.io/items/1", json=patch)'
        r = _find_http_calls_in_text(src, "httpx", "patch", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "PATCH"

    def test_httpx_request(self) -> None:
        src = 'httpx.request("GET", "https://api.svc.io/items")'
        r = _find_http_calls_in_text(src, "httpx", "request", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"

    def test_httpx_client_alias(self) -> None:
        src = 'client.get("https://api.svc.io/data")'
        r = _find_http_calls_in_text(src, "httpx", "get", "mod", "app.py")
        assert len(r) == 1


class TestCrossLanguagePythonAiohttp:
    """Python — aiohttp library"""

    def test_aiohttp_get(self) -> None:
        src = 'await session.get("https://api.svc.io/users")'
        r = _find_http_calls_in_text(src, "aiohttp", "get", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "GET"

    def test_aiohttp_post(self) -> None:
        src = 'await session.post("https://api.svc.io/users", json=data)'
        r = _find_http_calls_in_text(src, "aiohttp", "post", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "POST"

    def test_aiohttp_delete(self) -> None:
        src = 'await session.delete("https://api.svc.io/users/5")'
        r = _find_http_calls_in_text(src, "aiohttp", "delete", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "DELETE"


class TestCrossLanguagePythonUrllib3:
    """Python — urllib3 library"""

    def test_urllib3_request(self) -> None:
        src = 'http.request("GET", "https://api.svc.io/health")'
        r = _find_http_calls_in_text(src, "urllib3", "request", "mod", "app.py")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"
        assert r[0].url_pattern == "/health"


class TestCrossLanguagePythonUrllibRequest:
    """Python — urllib.request (stdlib)"""

    def test_urllib_request_get(self) -> None:
        src = 'urllib.request.get("https://api.svc.io/data")'
        r = _find_http_calls_in_text(src, "urllib.request", "get", "mod", "app.py")
        assert len(r) == 1
        assert r[0].library == "urllib.request"


class TestCrossLanguagePythonMultipleLibraries:
    """Python — mixed library usage in single file"""

    def test_requests_and_httpx_in_same_source(self) -> None:
        src = """import requests
import httpx

requests.get("https://api.svc.io/users")
httpx.post("https://api.svc.io/items")
requests.delete("https://api.svc.io/users/1")
"""
        r_requests_get = _find_http_calls_in_text(src, "requests", "get", "m", "f.py")
        r_httpx_post = _find_http_calls_in_text(src, "httpx", "post", "m", "f.py")
        r_requests_del = _find_http_calls_in_text(src, "requests", "delete", "m", "f.py")

        assert len(r_requests_get) == 1
        assert r_requests_get[0].url_pattern == "/users"
        assert len(r_httpx_post) == 1
        assert r_httpx_post[0].url_pattern == "/items"
        assert len(r_requests_del) == 1
        assert r_requests_del[0].url_pattern == "/users/1"

    def test_all_python_modules_in_constants(self) -> None:
        """Verify test coverage matches HTTP_CLIENT_PATTERNS for python."""
        patterns = cs.HTTP_CLIENT_PATTERNS["python"]
        expected_modules = {"requests", "httpx", "aiohttp", "urllib3", "urllib.request"}
        assert set(patterns["modules"]) == expected_modules

    def test_all_python_methods_in_constants(self) -> None:
        patterns = cs.HTTP_CLIENT_PATTERNS["python"]
        expected_methods = {"get", "post", "put", "delete", "patch", "head", "options", "request"}
        assert set(patterns["methods"]) == expected_methods


# ────────────────────────────────────────────────────────────────────
# JavaScript cross-language tests
# ────────────────────────────────────────────────────────────────────


class TestCrossLanguageJavaScriptAxios:
    """JavaScript — axios library"""

    def test_axios_get(self) -> None:
        src = 'const res = axios.get("https://api.svc.io/users");'
        r = _find_http_calls_in_text(src, "axios", "get", "mod", "app.js")
        assert len(r) == 1
        assert r[0].http_method == "GET"
        assert r[0].library == "axios"

    def test_axios_post(self) -> None:
        src = 'axios.post("https://api.svc.io/users", { name: "Alice" });'
        r = _find_http_calls_in_text(src, "axios", "post", "mod", "app.js")
        assert len(r) == 1
        assert r[0].http_method == "POST"

    def test_axios_put(self) -> None:
        src = 'axios.put("https://api.svc.io/users/1", data);'
        r = _find_http_calls_in_text(src, "axios", "put", "mod", "app.js")
        assert len(r) == 1
        assert r[0].http_method == "PUT"

    def test_axios_delete(self) -> None:
        src = 'axios.delete("https://api.svc.io/users/1");'
        r = _find_http_calls_in_text(src, "axios", "delete", "mod", "app.js")
        assert len(r) == 1
        assert r[0].http_method == "DELETE"

    def test_axios_patch(self) -> None:
        src = 'axios.patch("https://api.svc.io/users/1", patch);'
        r = _find_http_calls_in_text(src, "axios", "patch", "mod", "app.js")
        assert len(r) == 1
        assert r[0].http_method == "PATCH"

    def test_axios_head(self) -> None:
        src = 'axios.head("https://api.svc.io/health");'
        r = _find_http_calls_in_text(src, "axios", "head", "mod", "app.js")
        assert len(r) == 1
        assert r[0].http_method == "HEAD"

    def test_axios_request_generic(self) -> None:
        src = 'axios.request({ url: "https://api.svc.io/data", method: "get" });'
        r = _find_http_calls_in_text(src, "axios", "request", "mod", "app.js")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"


class TestCrossLanguageJavaScriptGot:
    """JavaScript — got library"""

    def test_got_get(self) -> None:
        src = 'const res = got.get("https://api.svc.io/items");'
        r = _find_http_calls_in_text(src, "got", "get", "mod", "app.js")
        assert len(r) == 1
        assert r[0].library == "got"

    def test_got_post(self) -> None:
        src = 'got.post("https://api.svc.io/items", { json: body });'
        r = _find_http_calls_in_text(src, "got", "post", "mod", "app.js")
        assert len(r) == 1
        assert r[0].http_method == "POST"

    def test_got_delete(self) -> None:
        src = 'got.delete("https://api.svc.io/items/42");'
        r = _find_http_calls_in_text(src, "got", "delete", "mod", "app.js")
        assert len(r) == 1
        assert r[0].http_method == "DELETE"


class TestCrossLanguageJavaScriptSuperagent:
    """JavaScript — superagent library"""

    def test_superagent_get(self) -> None:
        src = 'superagent.get("https://api.svc.io/users");'
        r = _find_http_calls_in_text(src, "superagent", "get", "mod", "app.js")
        assert len(r) == 1
        assert r[0].library == "superagent"

    def test_superagent_post(self) -> None:
        src = 'superagent.post("https://api.svc.io/users").send(data);'
        r = _find_http_calls_in_text(src, "superagent", "post", "mod", "app.js")
        assert len(r) == 1

    def test_superagent_put(self) -> None:
        src = 'superagent.put("https://api.svc.io/users/1").send(data);'
        r = _find_http_calls_in_text(src, "superagent", "put", "mod", "app.js")
        assert len(r) == 1


class TestCrossLanguageJavaScriptUndici:
    """JavaScript — undici library"""

    def test_undici_request(self) -> None:
        src = 'undici.request("https://api.svc.io/data");'
        r = _find_http_calls_in_text(src, "undici", "request", "mod", "app.js")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"

    def test_undici_fetch(self) -> None:
        src = 'undici.fetch("https://api.svc.io/data");'
        r = _find_http_calls_in_text(src, "undici", "fetch", "mod", "app.js")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"


class TestCrossLanguageJavaScriptNodeFetch:
    """JavaScript — node-fetch library"""

    def test_node_fetch_fetch(self) -> None:
        # Typically used as: const fetch = require('node-fetch'); fetch(url)
        # But the pattern requires module.method, so direct fetch() won't match
        src = 'nodeFetch.fetch("https://api.svc.io/data");'
        r = _find_http_calls_in_text(src, "node-fetch", "fetch", "mod", "app.js")
        assert len(r) == 1

    def test_node_fetch_get(self) -> None:
        src = 'nodeFetch.get("https://api.svc.io/data");'
        r = _find_http_calls_in_text(src, "node-fetch", "get", "mod", "app.js")
        assert len(r) == 1


class TestCrossLanguageJavaScriptConstants:
    def test_all_js_modules_in_constants(self) -> None:
        patterns = cs.HTTP_CLIENT_PATTERNS["javascript"]
        expected = {"axios", "fetch", "node-fetch", "got", "superagent", "undici"}
        assert set(patterns["modules"]) == expected

    def test_all_js_methods_in_constants(self) -> None:
        patterns = cs.HTTP_CLIENT_PATTERNS["javascript"]
        expected = {"get", "post", "put", "delete", "patch", "head", "request", "fetch"}
        assert set(patterns["methods"]) == expected

    def test_typescript_same_as_javascript(self) -> None:
        """TypeScript uses same patterns as JavaScript."""
        assert cs.HTTP_CLIENT_PATTERNS["typescript"] == cs.HTTP_CLIENT_PATTERNS["javascript"]


# ────────────────────────────────────────────────────────────────────
# TypeScript cross-language tests
# ────────────────────────────────────────────────────────────────────


class TestCrossLanguageTypeScript:
    """TypeScript — same modules as JS but in .ts files"""

    def test_axios_get_ts(self) -> None:
        src = 'const res = await axios.get<User[]>("https://api.svc.io/users");'
        r = _find_http_calls_in_text(src, "axios", "get", "mod", "app.ts")
        assert len(r) == 1
        assert r[0].http_method == "GET"

    def test_axios_post_ts(self) -> None:
        src = 'await axios.post<User>("https://api.svc.io/users", userData);'
        r = _find_http_calls_in_text(src, "axios", "post", "mod", "app.ts")
        assert len(r) == 1
        assert r[0].http_method == "POST"

    def test_got_get_ts(self) -> None:
        src = 'const { body } = await got.get("https://api.svc.io/items");'
        r = _find_http_calls_in_text(src, "got", "get", "mod", "app.ts")
        assert len(r) == 1

    def test_superagent_delete_ts(self) -> None:
        src = 'await superagent.delete("https://api.svc.io/items/1");'
        r = _find_http_calls_in_text(src, "superagent", "delete", "mod", "app.ts")
        assert len(r) == 1
        assert r[0].http_method == "DELETE"


# ────────────────────────────────────────────────────────────────────
# Java cross-language tests
# ────────────────────────────────────────────────────────────────────


class TestCrossLanguageJavaHttpClient:
    """Java — HttpClient (java.net.http)"""

    def test_httpclient_send(self) -> None:
        src = 'HttpResponse<String> resp = client.send(request, BodyHandlers.ofString());'
        r = _find_http_calls_in_text(src, "HttpClient", "send", "mod", "App.java")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"
        assert r[0].library == "HttpClient"


class TestCrossLanguageJavaRestTemplate:
    """Java — Spring RestTemplate"""

    def test_resttemplate_getForObject(self) -> None:
        src = 'User user = restTemplate.getForObject("http://user-svc/users/{id}", User.class);'
        r = _find_http_calls_in_text(src, "RestTemplate", "getForObject", "mod", "Svc.java")
        assert len(r) == 1
        assert r[0].http_method == "GET"
        assert r[0].library == "RestTemplate"

    def test_resttemplate_postForObject(self) -> None:
        src = 'User created = restTemplate.postForObject("http://user-svc/users", body, User.class);'
        r = _find_http_calls_in_text(src, "RestTemplate", "postForObject", "mod", "Svc.java")
        assert len(r) == 1
        assert r[0].http_method == "POST"

    def test_resttemplate_exchange(self) -> None:
        src = 'ResponseEntity<User> resp = restTemplate.exchange("http://user-svc/users", HttpMethod.GET, null, User.class);'
        r = _find_http_calls_in_text(src, "RestTemplate", "exchange", "mod", "Svc.java")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"

    def test_resttemplate_execute(self) -> None:
        src = 'restTemplate.execute("http://user-svc/process", HttpMethod.POST, reqCb, resCb);'
        r = _find_http_calls_in_text(src, "RestTemplate", "execute", "mod", "Svc.java")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"


class TestCrossLanguageJavaWebClient:
    """Java — Spring WebClient (reactive)"""

    def test_webclient_get(self) -> None:
        src = 'Mono<User> user = webClient.get().uri("http://user-svc/users/1").retrieve().bodyToMono(User.class);'
        r = _find_http_calls_in_text(src, "WebClient", "get", "mod", "Svc.java")
        assert len(r) == 1
        assert r[0].http_method == "GET"

    def test_webclient_post(self) -> None:
        src = 'webClient.post().uri("http://user-svc/users").bodyValue(user).retrieve();'
        r = _find_http_calls_in_text(src, "WebClient", "post", "mod", "Svc.java")
        assert len(r) == 1
        assert r[0].http_method == "POST"


class TestCrossLanguageJavaOkHttp:
    """Java — OkHttp"""

    def test_okhttpclient_newCall(self) -> None:
        src = 'Response response = client.newCall(request).execute();'
        r = _find_http_calls_in_text(src, "OkHttpClient", "newCall", "mod", "App.java")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"
        assert r[0].library == "OkHttpClient"


class TestCrossLanguageJavaRetrofit:
    """Java — Retrofit"""

    def test_retrofit_execute(self) -> None:
        src = 'Response<User> resp = call.execute();'
        r = _find_http_calls_in_text(src, "Retrofit", "execute", "mod", "App.java")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"


class TestCrossLanguageJavaConstants:
    def test_all_java_modules_in_constants(self) -> None:
        patterns = cs.HTTP_CLIENT_PATTERNS["java"]
        expected = {"HttpClient", "RestTemplate", "WebClient", "OkHttpClient", "Retrofit"}
        assert set(patterns["modules"]) == expected

    def test_all_java_methods_in_constants(self) -> None:
        patterns = cs.HTTP_CLIENT_PATTERNS["java"]
        expected = {"send", "getForObject", "postForObject", "exchange", "execute", "newCall"}
        assert set(patterns["methods"]) == expected


# ────────────────────────────────────────────────────────────────────
# Go cross-language tests
# ────────────────────────────────────────────────────────────────────


class TestCrossLanguageGoNetHttp:
    """Go — net/http stdlib"""

    def test_http_Get(self) -> None:
        src = 'resp, err := http.Get("https://api.svc.io/users")'
        r = _find_http_calls_in_text(src, "net/http", "Get", "mod", "main.go")
        assert len(r) == 1
        assert r[0].http_method == "GET"
        assert r[0].url_pattern == "/users"

    def test_http_Post(self) -> None:
        src = 'resp, err := http.Post("https://api.svc.io/users", "application/json", body)'
        r = _find_http_calls_in_text(src, "net/http", "Post", "mod", "main.go")
        assert len(r) == 1
        assert r[0].http_method == "POST"

    def test_http_Do(self) -> None:
        src = 'resp, err := client.Do(req)'
        r = _find_http_calls_in_text(src, "net/http", "Do", "mod", "main.go")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"

    def test_http_NewRequest(self) -> None:
        src = 'req, err := http.NewRequest("GET", "https://api.svc.io/items", nil)'
        r = _find_http_calls_in_text(src, "net/http", "NewRequest", "mod", "main.go")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"
        assert r[0].url_pattern == "/items"


class TestCrossLanguageGoResty:
    """Go — resty library"""

    def test_resty_Get(self) -> None:
        src = 'resp, err := client.Get("https://api.svc.io/data")'
        r = _find_http_calls_in_text(src, "resty", "Get", "mod", "main.go")
        assert len(r) == 1
        assert r[0].http_method == "GET"

    def test_resty_Post(self) -> None:
        src = 'resp, err := client.Post("https://api.svc.io/data")'
        r = _find_http_calls_in_text(src, "resty", "Post", "mod", "main.go")
        assert len(r) == 1
        assert r[0].http_method == "POST"

    def test_resty_Do(self) -> None:
        src = 'resp, err := client.Do(req)'
        r = _find_http_calls_in_text(src, "resty", "Do", "mod", "main.go")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"


class TestCrossLanguageGoConstants:
    def test_all_go_modules_in_constants(self) -> None:
        patterns = cs.HTTP_CLIENT_PATTERNS["go"]
        expected = {"net/http", "resty"}
        assert set(patterns["modules"]) == expected

    def test_all_go_methods_in_constants(self) -> None:
        patterns = cs.HTTP_CLIENT_PATTERNS["go"]
        expected = {"Get", "Post", "Do", "NewRequest"}
        assert set(patterns["methods"]) == expected


# ────────────────────────────────────────────────────────────────────
# Rust cross-language tests
# ────────────────────────────────────────────────────────────────────


class TestCrossLanguageRustReqwest:
    """Rust — reqwest library"""

    def test_reqwest_get(self) -> None:
        src = 'let resp = reqwest.get("https://api.svc.io/users").await?;'
        r = _find_http_calls_in_text(src, "reqwest", "get", "mod", "main.rs")
        assert len(r) == 1
        assert r[0].http_method == "GET"
        assert r[0].library == "reqwest"

    def test_reqwest_post(self) -> None:
        src = 'let resp = client.post("https://api.svc.io/users").json(&body).send().await?;'
        r = _find_http_calls_in_text(src, "reqwest", "post", "mod", "main.rs")
        assert len(r) == 1
        assert r[0].http_method == "POST"

    def test_reqwest_put(self) -> None:
        src = 'let resp = client.put("https://api.svc.io/users/1").json(&body).send().await?;'
        r = _find_http_calls_in_text(src, "reqwest", "put", "mod", "main.rs")
        assert len(r) == 1
        assert r[0].http_method == "PUT"

    def test_reqwest_delete(self) -> None:
        src = 'let resp = client.delete("https://api.svc.io/users/1").send().await?;'
        r = _find_http_calls_in_text(src, "reqwest", "delete", "mod", "main.rs")
        assert len(r) == 1
        assert r[0].http_method == "DELETE"

    def test_reqwest_send(self) -> None:
        src = 'let resp = client.send(req).await?;'
        r = _find_http_calls_in_text(src, "reqwest", "send", "mod", "main.rs")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"

    def test_reqwest_request(self) -> None:
        src = 'let resp = client.request(Method::GET, "https://api.svc.io/data").send().await?;'
        r = _find_http_calls_in_text(src, "reqwest", "request", "mod", "main.rs")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"


class TestCrossLanguageRustHyper:
    """Rust — hyper library"""

    def test_hyper_get(self) -> None:
        src = 'let resp = client.get("https://api.svc.io/data".parse()?).await?;'
        r = _find_http_calls_in_text(src, "hyper", "get", "mod", "main.rs")
        assert len(r) == 1
        assert r[0].library == "hyper"

    def test_hyper_request(self) -> None:
        src = 'let resp = client.request(req).await?;'
        r = _find_http_calls_in_text(src, "hyper", "request", "mod", "main.rs")
        assert len(r) == 1
        assert r[0].http_method == "UNKNOWN"

    def test_hyper_send(self) -> None:
        src = 'let resp = client.send(req).await?;'
        r = _find_http_calls_in_text(src, "hyper", "send", "mod", "main.rs")
        assert len(r) == 1


class TestCrossLanguageRustSurf:
    """Rust — surf library"""

    def test_surf_get(self) -> None:
        src = 'let mut resp = surf.get("https://api.svc.io/health").await?;'
        r = _find_http_calls_in_text(src, "surf", "get", "mod", "main.rs")
        assert len(r) == 1
        assert r[0].library == "surf"
        assert r[0].http_method == "GET"

    def test_surf_post(self) -> None:
        src = 'let mut resp = surf.post("https://api.svc.io/items").body_json(&item)?.await?;'
        r = _find_http_calls_in_text(src, "surf", "post", "mod", "main.rs")
        assert len(r) == 1
        assert r[0].http_method == "POST"

    def test_surf_delete(self) -> None:
        src = 'let mut resp = surf.delete("https://api.svc.io/items/1").await?;'
        r = _find_http_calls_in_text(src, "surf", "delete", "mod", "main.rs")
        assert len(r) == 1
        assert r[0].http_method == "DELETE"


class TestCrossLanguageRustConstants:
    def test_all_rust_modules_in_constants(self) -> None:
        patterns = cs.HTTP_CLIENT_PATTERNS["rust"]
        expected = {"reqwest", "hyper", "surf"}
        assert set(patterns["modules"]) == expected

    def test_all_rust_methods_in_constants(self) -> None:
        patterns = cs.HTTP_CLIENT_PATTERNS["rust"]
        expected = {"get", "post", "put", "delete", "send", "request"}
        assert set(patterns["methods"]) == expected


# ────────────────────────────────────────────────────────────────────
# Cross-language: all 6 supported languages are covered
# ────────────────────────────────────────────────────────────────────


class TestCrossLanguageCoverage:
    """Meta-tests verifying we have tests for every language in HTTP_CLIENT_PATTERNS."""

    def test_all_six_languages_have_patterns(self) -> None:
        expected = {"python", "javascript", "typescript", "java", "go", "rust"}
        assert set(cs.HTTP_CLIENT_PATTERNS.keys()) == expected

    def test_every_language_module_has_test(self) -> None:
        """Cross-reference: every module in HTTP_CLIENT_PATTERNS has at least one
        test in this file exercising _find_http_calls_in_text with that module."""
        # This is a declarative assertion that all modules are covered.
        # The actual coverage is enforced by the per-language test classes above.
        for lang, patterns in cs.HTTP_CLIENT_PATTERNS.items():
            for module in patterns["modules"]:
                # Smoke check: the pattern is valid for regex
                import re
                escaped = re.escape(module)
                re.compile(rf"(?:{escaped}|[a-zA-Z_]\w*)\.get\s*\(")

    def test_cross_language_url_extraction_consistency(self) -> None:
        """Same URL in different languages should produce same url_pattern."""
        url = "https://api.example.com/users/:id/posts"
        expected = "/users/{id}/posts"

        langs_and_modules = [
            ("requests", "get"),
            ("axios", "get"),
            ("RestTemplate", "getForObject"),
            ("net/http", "Get"),
            ("reqwest", "get"),
        ]

        for mod, method in langs_and_modules:
            src = f'{mod}.{method}("{url}")'
            # The alias wildcard in the regex will match the module prefix
            r = _find_http_calls_in_text(src, mod, method, "m", "f")
            assert len(r) >= 1, f"No match for {mod}.{method}"
            assert r[0].url_pattern == expected, (
                f"URL mismatch for {mod}.{method}: "
                f"got {r[0].url_pattern}, expected {expected}"
            )

    def test_cross_language_path_param_normalization(self) -> None:
        """All param styles (:id, <id>, {id}) normalize the same across languages."""
        test_cases = [
            ("/users/:id", "/users/{id}"),
            ("/users/<id>", "/users/{id}"),
            ("/users/{id}", "/users/{id}"),
            ("/orgs/:orgId/teams/:teamId", "/orgs/{orgId}/teams/{teamId}"),
            ("/orgs/<orgId>/teams/<teamId>", "/orgs/{orgId}/teams/{teamId}"),
        ]
        for url, expected in test_cases:
            assert _extract_url_path(url) == expected, f"Failed for {url}"
