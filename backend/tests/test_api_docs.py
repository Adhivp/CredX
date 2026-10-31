"""Catch stale exported schemas and missing endpoint documentation."""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from scripts.export_openapi import isolated_app

    with isolated_app() as app, TestClient(app) as client:
        yield client


@pytest.mark.parametrize("scratch_source", ["runner", "home"])
def test_export_without_tmpdir(tmp_path, monkeypatch, scratch_source):
    from scripts.export_openapi import isolated_app

    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("RUNNER_TEMP", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = tmp_path / ".cache" / "credx"
    if scratch_source == "runner":
        expected = tmp_path / "runner"
        monkeypatch.setenv("RUNNER_TEMP", str(expected))
    with isolated_app() as app:
        database = Path(app.state.engine.url.database)
        assert database.is_relative_to(expected)
        assert database.is_file()
    assert not database.exists()


def test_export_ignores_runtime_settings(tmp_path, monkeypatch):
    from app.core.config import Settings
    from scripts.export_openapi import export_schema

    dotenv = tmp_path / "synthetic.env"
    dotenv.write_text("JWT_SECRET=invalid\nCORS_ORIGINS=invalid-json\n")
    monkeypatch.setitem(Settings.model_config, "env_file", dotenv)
    monkeypatch.setenv("JWT_SECRET", "invalid")
    monkeypatch.setenv("CORS_ORIGINS", "invalid-json")
    forbidden_db = tmp_path / "must-not-exist.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{forbidden_db}")
    output = tmp_path / "openapi.json"
    export_schema(output)
    first = json.loads(output.read_text())
    export_schema(output)
    assert json.loads(output.read_text()) == first
    assert first["info"]["title"] == "CredX API"
    assert not forbidden_db.exists()


def test_api_documents_match_application(client):
    docs = Path(__file__).resolve().parents[1] / "docs"
    schema_file = docs / "openapi.json"
    guide_file = docs / "API.md"
    assert schema_file.is_file(), "Exported OpenAPI schema is missing"
    assert guide_file.is_file(), "API reference is missing"
    schema = json.loads(schema_file.read_text())
    assert schema == client.get("/openapi.json").json()
    methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    operations = {
        (method.upper(), path)
        for path, item in schema["paths"].items()
        for method in item
        if method in methods
    }
    documented = set(
        re.findall(
            r"^### (GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE) (\S+)$",
            guide_file.read_text(),
            re.MULTILINE,
        )
    )
    assert documented == operations


def test_documented_ews_walkthrough():
    guide = Path(__file__).resolve().parents[1] / "docs" / "API.md"
    match = re.search(r"<!-- ews-walkthrough -->\s*```python\n(.*?)```", guide.read_text(), re.S)
    assert match, "Executable isolated EWS walkthrough is missing"
    exec(compile(match[1], str(guide), "exec"), {})
