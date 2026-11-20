"""Prism's scripts only call AccountService methods that still exist.

Dify moves AccountService helpers out between releases (1.17 moved the
email-register token helpers to an adapter); a removed method only fails when
that request path runs, so check every script statically instead.
"""

import ast
from pathlib import Path

from services.account_service import AccountService

SCRIPTS_DIR = Path(__file__).resolve().parents[4] / "scripts"


def _account_service_attributes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "AccountService"
    }


def test_scripts_only_call_existing_account_service_methods():
    missing = {
        str(path.relative_to(SCRIPTS_DIR)): sorted(attrs)
        for path in sorted(SCRIPTS_DIR.rglob("*.py"))
        if (attrs := {a for a in _account_service_attributes(path) if not hasattr(AccountService, a)})
    }

    assert missing == {}
