"""Catch stale exported schemas and missing endpoint documentation."""

import json
import re
from pathlib import Path


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
