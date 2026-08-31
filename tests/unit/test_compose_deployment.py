"""Validation tests for the bundled Docker Compose deployment.

These are pure parsing checks (no Docker daemon required) that guard the
production-readiness guarantees described in ``docs/docker.md``: persistent
PostgreSQL storage, a database healthcheck, restart policies, and the
dev-only example password override.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"


def _load_compose() -> dict:
    with COMPOSE_FILE.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_compose_file_exists_and_parses() -> None:
    compose = _load_compose()
    assert isinstance(compose, dict)
    assert "postgres" in compose["services"]
    assert "dischat" in compose["services"]


def test_postgres_data_uses_named_volume() -> None:
    compose = _load_compose()

    volumes = compose["services"]["postgres"]["volumes"]
    mounts = [str(mount) for mount in volumes if str(mount).endswith(":/var/lib/postgresql/data")]
    assert mounts, "postgres must mount a volume at /var/lib/postgresql/data"

    volume_name = mounts[0].split(":")[0]
    assert volume_name in compose.get("volumes", {}), (
        f"volume {volume_name!r} must be declared as a named top-level volume"
    )


def test_postgres_has_healthcheck() -> None:
    healthcheck = _load_compose()["services"]["postgres"].get("healthcheck")
    assert healthcheck is not None
    assert "pg_isready" in " ".join(healthcheck["test"])


def test_services_restart_policy_is_unless_stopped() -> None:
    services = _load_compose()["services"]
    for name, service in services.items():
        assert service.get("restart") == "unless-stopped", (
            f"{name} must use restart: unless-stopped"
        )


def test_postgres_password_is_required_not_silently_defaulted() -> None:
    environment = _load_compose()["services"]["postgres"]["environment"]
    value = environment.get("POSTGRES_PASSWORD", "")
    assert "${POSTGRES_PASSWORD:?" in value, (
        "POSTGRES_PASSWORD must fail fast when unset/empty so production "
        "volumes cannot initialize with the known dev example password"
    )
    assert "dev-only" in value or "dischat" in value, (
        "the error message must point operators at the dev-only example guidance"
    )
