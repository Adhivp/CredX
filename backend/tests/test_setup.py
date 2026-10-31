import stat
from pathlib import Path


def test_environment_generator_creates_private_secret_without_overwriting(tmp_path):
    assert Path("scripts/init_env.py").is_file(), "Environment initializer is missing"
    from scripts.init_env import create_env

    destination = tmp_path / ".env"
    assert create_env(destination) is True
    content = destination.read_text()
    secret = content.splitlines()[0].split("=", 1)[1]
    assert len(secret) >= 48
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert create_env(destination) is False
    assert destination.read_text() == content
