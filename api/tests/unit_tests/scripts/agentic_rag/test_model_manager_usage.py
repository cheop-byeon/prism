"""Prism's scripts build ModelManager the way Dify does since 1.14.

ModelManager() stopped taking zero arguments in Dify 1.14 (it needs a
ProviderManager); a bare call only fails at the moment an LLM is invoked, so
check every script statically instead.
"""

import ast
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[4] / "scripts"


def _bare_model_manager_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text())
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ModelManager"
        and not node.args
        and not node.keywords
    ]


def test_scripts_do_not_call_model_manager_without_a_provider_manager():
    offenders = {
        str(path.relative_to(SCRIPTS_DIR)): lines
        for path in sorted(SCRIPTS_DIR.rglob("*.py"))
        if (lines := _bare_model_manager_calls(path))
    }

    assert offenders == {}
