"""Exercise the actual image offline with a disposable database and signing key."""

import json
import os
import secrets
import subprocess
import sys
import time


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, text=True, **kwargs)


def main(image):
    name = "credx-smoke-" + secrets.token_hex(6)
    env = dict(os.environ, JWT_SECRET=secrets.token_urlsafe(64))
    try:
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            "none",
            "--env",
            "JWT_SECRET",
            image,
            env=env,
            capture_output=True,
        )
        probe = (
            "import json, urllib.request; "
            "r=urllib.request.urlopen('http://127.0.0.1:9000/health', timeout=3); "
            "assert r.status == 200 and json.load(r) == {'status':'ok'}"
        )
        for _ in range(60):
            result = subprocess.run(
                ["docker", "exec", name, "python", "-c", probe], capture_output=True, text=True
            )
            if result.returncode == 0:
                break
            if (
                docker(
                    "inspect", "--format", "{{.State.Running}}", name, capture_output=True
                ).stdout.strip()
                != "true"
            ):
                raise RuntimeError("Container exited before becoming ready")
            time.sleep(2)
        else:
            raise RuntimeError("Container did not become ready within 120 seconds")
        docker(
            "exec",
            name,
            "python",
            "-c",
            """
import json, os, sqlite3, sys, urllib.request
from pathlib import Path
from alembic.config import Config
from alembic.script import ScriptDirectory
assert sys.version_info[:2] == (3, 14)
assert os.getuid() == 10001
assert not Path('/app/.env').exists()
assert not Path('/app/Frontend').exists()
for path in ['/docs', '/openapi.json']:
    with urllib.request.urlopen('http://127.0.0.1:9000'+path, timeout=5) as r:
        assert r.status == 200
        if path.endswith('.json'):
            assert '/health' in json.load(r)['paths']
with sqlite3.connect('/app/data/credx.db') as db:
    versions = {r[0] for r in db.execute('select version_num from alembic_version')}
    assert versions == set(ScriptDirectory.from_config(Config('alembic.ini')).get_heads())
    assert db.execute('select count(*) from users').fetchone()[0] == 0
from app.main import create_app
from core.rules.repayment_delays import detect_repayment_delays
print('PASS: Python 3.14, non-root, port 9000, /health, /docs, /openapi.json')
print('PASS: migration head, empty users, shared rules')
""",
        )
        docker("exec", name, "python", "-m", "alembic", "upgrade", "head")
        docker("exec", name, "python", "-m", "alembic", "current")
        docker("stop", "--time", "15", name, capture_output=True)
        state = json.loads(
            docker("inspect", "--format", "{{json .State}}", name, capture_output=True).stdout
        )
        if state["ExitCode"] != 0:
            raise RuntimeError("Uvicorn did not shut down cleanly")
        print("PASS: repeat migration and SIGTERM shutdown")
    finally:
        subprocess.run(["docker", "logs", "--tail", "40", name], check=False)
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, check=False)


if __name__ == "__main__":
    main(sys.argv[1])
