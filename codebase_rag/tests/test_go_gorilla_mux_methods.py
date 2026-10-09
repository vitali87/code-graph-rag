"""gorilla/mux routes take their verb from the `.Methods(...)` chain (#3195).

`r.HandleFunc("/users", listUsers).Methods("GET")` was recorded as `ANY
/users`, so a GET and a POST handler on one path became one endpoint
exposed by both, and every client of the path was linked to both. The
module's own contract says an unreadable chain yields nothing, never a
wrong template.
"""

from __future__ import annotations

import pytest

from codebase_rag.tests.test_route_call_endpoints import _run

_MAIN = """\
package main

import (
\t"net/http"

\t"github.com/gorilla/mux"
)

func listUsers(w http.ResponseWriter, r *http.Request)  {}
func createUser(w http.ResponseWriter, r *http.Request) {}
func getUser(w http.ResponseWriter, r *http.Request)    {}
func deleteUser(w http.ResponseWriter, r *http.Request) {}
func putUser(w http.ResponseWriter, r *http.Request)    {}
func anyUser(w http.ResponseWriter, r *http.Request)    {}
func health(w http.ResponseWriter, r *http.Request)     {}
func dyn(w http.ResponseWriter, r *http.Request)        {}

func main() {
\tverb := pick()
\tr := mux.NewRouter()
\tr.HandleFunc("/users", listUsers).Methods("GET")
\tr.HandleFunc("/users", createUser).Methods(http.MethodPost)
\tr.HandleFunc("/users/{id}", getUser).Methods("GET", "HEAD")
\tr.HandleFunc("/users/{id}", deleteUser).Name("del").Methods(`DELETE`)
\tr.HandleFunc("/users/{id}/put", putUser).Methods("put")
\tr.HandleFunc("/users/{id}/any", anyUser)
\tr.HandleFunc("/dyn", dyn).Methods(verb)
\tm := http.NewServeMux()
\tm.HandleFunc("GET /health", health)
}

func pick() string { return "GET" }
"""


@pytest.fixture(scope="module")
def exposed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, set[str]]:
    edges = _run(tmp_path_factory.mktemp("gomux"), {"main.go": _MAIN}, "go")
    out: dict[str, set[str]] = {}
    for _label, qn, identity in edges:
        out.setdefault(qn.rsplit(".", 1)[-1], set()).add(identity)
    return out


@pytest.mark.parametrize(
    ("handler", "identities"),
    [
        ("listUsers", {"GET /users"}),
        ("createUser", {"POST /users"}),
        ("getUser", {"GET /users/{id}", "HEAD /users/{id}"}),
        ("deleteUser", {"DELETE /users/{id}"}),
        ("putUser", {"PUT /users/{id}/put"}),
    ],
    ids=[
        "string-verb",
        "http-method-constant",
        "several-verbs",
        "raw-string-after-another-link",
        "lowercase-verb",
    ],
)
def test_a_methods_chain_sets_the_verb(
    exposed: dict[str, set[str]], handler: str, identities: set[str]
) -> None:
    assert exposed.get(handler) == identities, exposed


def test_a_route_without_a_methods_chain_still_serves_every_verb(
    exposed: dict[str, set[str]],
) -> None:
    # Negatives: a bare HandleFunc is method-agnostic, and a Go 1.22
    # pattern still reads its own verb.
    assert exposed.get("anyUser") == {"ANY /users/{id}/any"}
    assert exposed.get("health") == {"GET /health"}


def test_an_unreadable_methods_chain_yields_no_endpoint(
    exposed: dict[str, set[str]],
) -> None:
    # Negative: a verb only known at run time is the documented ceiling:
    # nothing, rather than a wrong ANY template.
    assert "dyn" not in exposed, exposed
