"""Prism and Dify migrations form one alembic graph after each Dify sync.

A sync can bring in a Dify revision whose ID a Prism migration already uses
(alembic then silently keeps only one of them), or leave Dify's new chain as
a second head that needs a merge migration.
"""

import ast
from collections import Counter
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

VERSIONS_DIR = Path(__file__).resolve().parents[3] / "migrations" / "versions"


def _declared_revisions() -> list[str]:
    revisions: list[str] = []
    for path in VERSIONS_DIR.glob("*.py"):
        for line in path.read_text().splitlines():
            if line.startswith("revision = "):
                revisions.append(ast.literal_eval(line.removeprefix("revision = ")))
                break
    return revisions


def _script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(VERSIONS_DIR.parent))
    return ScriptDirectory.from_config(config)


def test_every_revision_id_is_unique():
    duplicates = [rev for rev, count in Counter(_declared_revisions()).items() if count > 1]

    assert duplicates == []


def test_migrations_have_a_single_head():
    assert len(_script_directory().get_heads()) == 1
