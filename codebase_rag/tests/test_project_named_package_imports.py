# (H) When the repo directory and its top-level package share a name (a django
# (H) clone is django/django/..., celery is celery/celery/...), a written
# (H) absolute import like `from django.http import HttpRequest` collides with
# (H) the project prefix: the import processor trusted the written path as an
# (H) already-qualified qn and returned it as-is, while every real node lives
# (H) under the DOUBLED prefix (django.django.http.request.HttpRequest). Every
# (H) absolute import in such a repo mapped to a nonexistent qn, severing
# (H) INHERITS (and with it OVERRIDES dispatch revival: django's
# (H) ASGIRequest._get_scheme, BaseUserCreationForm._post_clean,
# (H) ArrayField._choices_is_value all reported dead). The as-is reading is
# (H) only correct when NO project-named top-level directory exists (flat
# (H) layout where the repo root doubles as the installed package).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater

REQUEST_PY = """class HttpRequest:
    @property
    def scheme(self):
        return self._get_scheme()

    def _get_scheme(self):
        return "http"
"""

ASGI_PY = """from pkgrepo.http import HttpRequest


class ASGIRequest(HttpRequest):
    def _get_scheme(self):
        return super()._get_scheme()
"""

FORMS_MODELS_PY = """class BaseModelForm:
    def _post_clean(self):
        return 1


class ModelForm(BaseModelForm):
    pass
"""

AUTH_FORMS_PY = """from pkgrepo import forms


class BaseUserCreationForm(forms.ModelForm):
    def _post_clean(self):
        return 2
"""


def _edges(mock_ingestor: MagicMock, rel_type: str) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel_type)
    }


def _build(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project = temp_repo / "pkgrepo"
    pkg = project / "pkgrepo"
    (pkg / "http").mkdir(parents=True)
    (pkg / "forms").mkdir()
    (pkg / "auth").mkdir()
    (project / "docs").mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "http" / "__init__.py").write_text(
        "from pkgrepo.http.request import HttpRequest\n", encoding="utf-8"
    )
    (pkg / "http" / "request.py").write_text(REQUEST_PY, encoding="utf-8")
    (pkg / "asgi.py").write_text(ASGI_PY, encoding="utf-8")
    (pkg / "forms" / "__init__.py").write_text(
        "from pkgrepo.forms.models import *  # noqa: F403\n", encoding="utf-8"
    )
    (pkg / "forms" / "models.py").write_text(FORMS_MODELS_PY, encoding="utf-8")
    (pkg / "auth" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "auth" / "forms.py").write_text(AUTH_FORMS_PY, encoding="utf-8")
    run_updater(project, mock_ingestor, skip_if_missing="python")


def test_bare_name_base_imported_through_package_reexport_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `from pkgrepo.http import HttpRequest` + `class ASGIRequest(HttpRequest)`
    # (H) must land on the class behind the package __init__'s explicit re-export.
    _build(temp_repo, mock_ingestor)

    assert (
        "pkgrepo.pkgrepo.asgi.ASGIRequest",
        "pkgrepo.pkgrepo.http.request.HttpRequest",
    ) in _edges(mock_ingestor, "INHERITS")


def test_override_through_collided_import_is_recorded(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _build(temp_repo, mock_ingestor)

    assert (
        "pkgrepo.pkgrepo.asgi.ASGIRequest._get_scheme",
        "pkgrepo.pkgrepo.http.request.HttpRequest._get_scheme",
    ) in _edges(mock_ingestor, "OVERRIDES")


def test_package_attribute_base_through_collided_import_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `from pkgrepo import forms` + `forms.ModelForm`: the #688 star-reexport
    # (H) machinery only fires once the import maps to the project-prefixed
    # (H) package qn instead of the written path.
    _build(temp_repo, mock_ingestor)

    assert (
        "pkgrepo.pkgrepo.auth.forms.BaseUserCreationForm",
        "pkgrepo.pkgrepo.forms.models.ModelForm",
    ) in _edges(mock_ingestor, "INHERITS")
    assert (
        "pkgrepo.pkgrepo.auth.forms.BaseUserCreationForm._post_clean",
        "pkgrepo.pkgrepo.forms.models.BaseModelForm._post_clean",
    ) in _edges(mock_ingestor, "OVERRIDES")


def test_flat_layout_self_named_import_keeps_written_path(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) No project-named top-level dir: the repo root itself doubles as the
    # (H) installed package (package_dir={'flatpkg': '.'}), so the written path
    # (H) already IS the qn and must stay untouched.
    project = temp_repo / "flatpkg"
    project.mkdir()
    (project / "models.py").write_text(
        "class Base:\n    def hook(self):\n        return 1\n", encoding="utf-8"
    )
    (project / "user.py").write_text(
        "from flatpkg.models import Base\n\n\n"
        "class User(Base):\n"
        "    def hook(self):\n"
        "        return 2\n",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing="python")

    assert ("flatpkg.user.User", "flatpkg.models.Base") in _edges(
        mock_ingestor, "INHERITS"
    )
