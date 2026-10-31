"""Export the real factory's OpenAPI without reading runtime secrets or databases.

Run from backend/: python -m scripts.export_openapi
Scratch uses TMPDIR, RUNNER_TEMP, or ~/.cache/credx. No live server is required.
"""

import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI


@contextmanager
def isolated_app() -> Iterator[FastAPI]:
    """Build the unchanged app with dotenv disabled and disposable settings.

    Environment patches are process-local; use this development helper in a
    dedicated process, not alongside a running application in the same process.
    """
    scratch = Path(
        os.environ.get("TMPDIR")
        or os.environ.get("RUNNER_TEMP")
        or Path.home() / ".cache" / "credx"
    ).resolve()
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="credx-api-docs-", dir=scratch) as directory:
        with patch.dict(os.environ, {"TMPDIR": str(scratch)}, clear=True):
            from app.core.config import Settings
            from app.main import create_app

            settings = Settings(
                _env_file=None,
                jwt_secret=secrets.token_urlsafe(48),
                database_url=f"sqlite:///{Path(directory) / 'docs.db'}",
                nebius_api_key="",
            )
            with patch("app.main.Settings", return_value=settings):
                app = create_app()
            try:
                yield app
            finally:
                app.state.engine.dispose()


def export_schema(output: Path) -> None:
    """Write generated JSON unchanged apart from deterministic formatting."""
    with isolated_app() as app:
        schema = app.openapi()
    output.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    destination = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
    export_schema(destination)
    print(f"Exported {destination}")
