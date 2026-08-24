"""`cgr help` must work with both real and vendored click (#1409).

Newer typer generations vendor click as `typer._click`, so the app's root
command no longer descends from the real `click.Group` and usage errors raise
the vendored `ClickException`. The help command duck-types the group and catches
both exception flavors; these tests pin the resolution helper's contract on
whichever typer is installed.
"""

from __future__ import annotations

import types

import click
import pytest

from codebase_rag import cli as cgr_cli


def test_real_click_exception_is_always_caught() -> None:
    assert click.ClickException in cgr_cli._CLICK_EXCEPTIONS


def test_without_vendored_module_real_click_is_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_import_error(name: str) -> types.ModuleType:
        raise ImportError(name)

    monkeypatch.setattr(cgr_cli.importlib, "import_module", raise_import_error)
    assert cgr_cli._vendored_click_exception() is click.ClickException


def test_vendored_exception_class_is_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class VendoredClickException(Exception):
        pass

    vendored = types.ModuleType("vendored_click_exceptions")
    vendored.ClickException = VendoredClickException
    monkeypatch.setattr(cgr_cli.importlib, "import_module", lambda _name: vendored)
    assert cgr_cli._vendored_click_exception() is VendoredClickException


def test_root_command_supports_duck_typed_group_resolution() -> None:
    import typer

    core = typer.main.get_command(cgr_cli.app)
    assert callable(getattr(core, "get_command", None))
