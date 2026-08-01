from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def dual_package_workspace(temp_repo: Path) -> Path:
    # A workspace SDK whose exports map sends `import` and `require` to
    # different source files, and a consumer that reaches it through a
    # destructured CommonJS `require()`. The require must resolve to the
    # CommonJS source, not the ESM one.
    project = temp_repo / "dual_package_workspace"
    project.mkdir()

    sdk = project / "packages" / "sdk"
    (sdk / "src" / "esm").mkdir(parents=True)
    (sdk / "src" / "cjs").mkdir(parents=True)
    (sdk / "package.json").write_text(
        encoding="utf-8",
        data="""
{
  "name": "@acme/sdk",
  "exports": {
    "./admin": {
      "import": "./src/esm/admin.js",
      "require": "./src/cjs/admin.js"
    }
  }
}
""",
    )
    (sdk / "src" / "esm" / "admin.js").write_text(
        encoding="utf-8",
        data="function adminEsm() {}\nmodule.exports = { adminEsm };\n",
    )
    (sdk / "src" / "cjs" / "admin.js").write_text(
        encoding="utf-8",
        data="function adminCjs() {}\nmodule.exports = { adminCjs };\n",
    )

    app = project / "app"
    app.mkdir()
    (app / "main.js").write_text(
        encoding="utf-8",
        data="const { adminCjs } = require('@acme/sdk/admin');\n"
        "function go() {\n  adminCjs();\n}\n",
    )
    return project


def test_destructured_require_resolves_the_commonjs_condition(
    dual_package_workspace: Path, mock_ingestor: MagicMock
) -> None:
    run_updater(dual_package_workspace, mock_ingestor, skip_if_missing="javascript")

    targets = {
        call.args[2][2]
        for call in get_relationships(mock_ingestor, "IMPORTS")
        if "app.main" in call.args[0][2]
    }

    assert any(target.endswith("cjs.admin") for target in targets), targets
    assert not any(target.endswith("esm.admin") for target in targets), targets
