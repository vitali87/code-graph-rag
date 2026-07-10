# (H) A base written as a PACKAGE attribute (`forms.ModelForm` via `from django
# (H) import forms`) names the re-exporting package, not the defining module
# (H) (django.forms.models.ModelForm re-exported by django/forms/__init__'s
# (H) star import). The deferred-INHERITS suffix match cannot bridge the missing
# (H) `.models` segment, so the edge was dropped -- and with it every OVERRIDES
# (H) relationship of the subclass (django BaseUserCreationForm._post_clean
# (H) reported dead).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater

FORMS_MODELS = """class BaseModelForm:
    def _post_clean(self):
        return 1


class ModelForm(BaseModelForm):
    pass
"""

FORMS_INIT = "from .models import *  # noqa: F403\n"

AUTH_FORMS = """from pkg import formslib as forms


class SetPasswordMixin:
    pass


class BaseUserCreationForm(SetPasswordMixin, forms.ModelForm):
    def _post_clean(self):
        return 2
"""


def _edges(mock_ingestor: MagicMock, rel_type: str) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel_type)
    }


def _build(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project = temp_repo / "reexp"
    (project / "pkg" / "formslib").mkdir(parents=True)
    (project / "pkg" / "auth").mkdir(parents=True)
    (project / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (project / "pkg" / "formslib" / "__init__.py").write_text(
        FORMS_INIT, encoding="utf-8"
    )
    (project / "pkg" / "formslib" / "models.py").write_text(
        FORMS_MODELS, encoding="utf-8"
    )
    (project / "pkg" / "auth" / "__init__.py").write_text("", encoding="utf-8")
    (project / "pkg" / "auth" / "forms.py").write_text(AUTH_FORMS, encoding="utf-8")
    run_updater(project, mock_ingestor, skip_if_missing="python")


def test_package_attribute_base_resolves_to_reexported_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _build(temp_repo, mock_ingestor)

    assert (
        "reexp.pkg.auth.forms.BaseUserCreationForm",
        "reexp.pkg.formslib.models.ModelForm",
    ) in _edges(mock_ingestor, "INHERITS")


def test_override_of_reexported_base_method_is_recorded(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _build(temp_repo, mock_ingestor)

    assert (
        "reexp.pkg.auth.forms.BaseUserCreationForm._post_clean",
        "reexp.pkg.formslib.models.BaseModelForm._post_clean",
    ) in _edges(mock_ingestor, "OVERRIDES")
