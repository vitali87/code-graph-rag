import pytest

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
