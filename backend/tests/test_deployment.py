"""Offline deployment contract checks; no Catalyst credentials required."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class DockerContract(unittest.TestCase):
    def test_workflow_exists(self):
        self.assertTrue((ROOT / ".github/workflows/deploy-catalyst.yml").is_file())

    def test_smoke_checker_exists(self):
        self.assertTrue((ROOT / "backend/scripts/smoke_container.py").is_file())

    def test_runtime_contract(self):
        path = ROOT / "backend/Dockerfile"
        self.assertTrue(path.is_file(), "Python 3.14 deployment Dockerfile is missing")
        text = path.read_text()
        self.assertIn("FROM python:3.14-slim", text)
        self.assertIn("WORKDIR /app", text)
        self.assertIn("--require-hashes", text)
        self.assertIn("EXPOSE 9000", text)
        self.assertIn("USER credx", text)
        command = json.loads(
            next(line[4:] for line in text.splitlines() if line.startswith("CMD "))
        )
        self.assertEqual(command[:2], ["sh", "-c"])
        self.assertIn("python -m alembic upgrade head &&", command[2])
        self.assertIn("exec python -m uvicorn app.main:create_app --factory", command[2])
        self.assertIn("--host 0.0.0.0 --port 9000", command[2])


if __name__ == "__main__":
    unittest.main()
