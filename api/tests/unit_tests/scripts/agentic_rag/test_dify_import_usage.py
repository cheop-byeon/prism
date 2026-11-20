"""Prism's scripts only import Dify names that still exist.

Dify moves service modules and helpers between releases (1.17 moved the
email-register token helpers to an adapter; 1.18 split AccountService into
services.account.*). Scripts import lazily inside request handlers, so a
removed module or name only fails when that request path runs; check every
script's first-party imports statically instead.
"""

import ast
import importlib
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[4] / "scripts"
FIRST_PARTY = ("constants", "controllers", "core", "extensions", "libs", "models", "repositories", "services")


def _first_party_imports(path: Path) -> set[tuple[str, str]]:
    tree = ast.parse(path.read_text())
    return {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
        if node.module.split(".")[0] in FIRST_PARTY
        for alias in node.names
    }


def _resolves(module: str, name: str) -> bool:
    try:
        imported = importlib.import_module(module)
    except ImportError:
        return False
    if hasattr(imported, name):
        return True
    try:
        importlib.import_module(f"{module}.{name}")
    except ImportError:
        return False
    return True


def test_scripts_only_import_existing_dify_names():
    missing = {
        str(path.relative_to(SCRIPTS_DIR)): sorted(f"{module}.{name}" for module, name in unresolved)
        for path in sorted(SCRIPTS_DIR.rglob("*.py"))
        if (unresolved := {imp for imp in _first_party_imports(path) if not _resolves(*imp)})
    }

    assert missing == {}
