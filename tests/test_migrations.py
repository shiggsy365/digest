import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

from digest.db import Base


def test_initial_migration_matches_models(tmp_path: Path) -> None:
    database = tmp_path / "migration.db"
    url = f"sqlite:///{database}"
    environment = {**os.environ, "DIGEST_DATABASE_URL": url}

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
    )

    engine = create_engine(url)
    actual_tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert actual_tables == set(Base.metadata.tables)

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        check=False,
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
