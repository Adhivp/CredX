"""Create a local signing key without printing it or overwriting existing configuration."""

import os
import secrets
from pathlib import Path


def create_env(path: Path) -> bool:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w") as stream:
        stream.write(f"JWT_SECRET={secrets.token_urlsafe(64)}\nCORS_ORIGINS=[]\n")
    return True


if __name__ == "__main__":
    destination = Path(__file__).resolve().parents[1] / ".env"
    created = create_env(destination)
    print(
        "Created backend/.env with a private random secret."
        if created
        else "Existing backend/.env preserved; no changes made."
    )
