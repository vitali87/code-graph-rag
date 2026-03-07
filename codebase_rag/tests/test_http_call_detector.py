from codebase_rag.parsers.http_call_detector import (
    _extract_url_path,
    _normalize_http_method,
)


class TestExtractUrlPath:
    def test_full_url(self) -> None:
        result = _extract_url_path("https://api.example.com/users/123")
        assert result == "/users/123"

    def test_path_only(self) -> None:
        result = _extract_url_path("/api/v1/items")
        assert result == "/api/v1/items"

    def test_colon_params(self) -> None:
        result = _extract_url_path("/users/:id/posts/:postId")
        assert result == "/users/{id}/posts/{postId}"

    def test_angle_bracket_params(self) -> None:
        result = _extract_url_path("/users/<user_id>")
        assert result == "/users/{user_id}"

    def test_curly_brace_params(self) -> None:
        result = _extract_url_path("/users/{userId}")
        assert result == "/users/{userId}"

    def test_empty_url(self) -> None:
        result = _extract_url_path("")
        assert result == "/"

    def test_just_slash(self) -> None:
        result = _extract_url_path("/")
        assert result == "/"


class TestNormalizeHttpMethod:
    def test_get(self) -> None:
        assert _normalize_http_method("get") == "GET"

    def test_post(self) -> None:
        assert _normalize_http_method("post") == "POST"

    def test_request(self) -> None:
        assert _normalize_http_method("request") == "UNKNOWN"

    def test_fetch(self) -> None:
        assert _normalize_http_method("fetch") == "UNKNOWN"

    def test_getForObject(self) -> None:
        assert _normalize_http_method("getForObject") == "GET"

    def test_case_insensitive(self) -> None:
        assert _normalize_http_method("GET") == "GET"
        assert _normalize_http_method("Post") == "POST"
