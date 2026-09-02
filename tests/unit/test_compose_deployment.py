"""Validation tests for the bundled Docker Compose deployment.

These are pure parsing checks (no Docker daemon required) that guard the
production-readiness guarantees described in ``docs/docker.md``: persistent
PostgreSQL storage, a database healthcheck, restart policies, and the
separation of bootstrap and runtime database roles.
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
        if name == "db-bootstrap":
            assert service.get("restart") == "no"
            continue
        assert service.get("restart") == "unless-stopped", (
            f"{name} must use restart: unless-stopped"
        )


def test_postgres_password_is_required_not_silently_defaulted() -> None:
    environment = _load_compose()["services"]["postgres"]["environment"]
    assert "${POSTGRES_ADMIN_PASSWORD:?" in environment.get("POSTGRES_PASSWORD", "")
    assert "${POSTGRES_PASSWORD:?" in environment.get("DISCHAT_DB_PASSWORD", "")


def test_postgres_runtime_role_is_not_the_bootstrap_superuser() -> None:
    compose = _load_compose()
    environment = compose["services"]["postgres"]["environment"]
    assert environment["POSTGRES_USER"] == "dischat_admin"
    mounts = compose["services"]["postgres"]["volumes"]
    assert any("init-runtime-user.sh" in str(mount) for mount in mounts)
    assert compose["services"]["dischat"]["environment"]["POSTGRES_ADMIN_PASSWORD"] == ""


def test_existing_volume_role_upgrade_runs_before_application() -> None:
    compose = _load_compose()
    bootstrap = compose["services"]["db-bootstrap"]
    assert bootstrap["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert compose["services"]["dischat"]["depends_on"]["db-bootstrap"]["condition"] == (
        "service_completed_successfully"
    )
    script = (REPO_ROOT / "docker/postgres/ensure-runtime-user.sh").read_text(encoding="utf-8")
    assert "legacy dischat owner" in script
    assert "NOSUPERUSER" in script


def test_runtime_config_is_mounted_instead_of_baked_into_image() -> None:
    mounts = _load_compose()["services"]["dischat"]["volumes"]
    config_mount = next(mount for mount in mounts if mount.get("target") == "/app/config.yaml")
    assert config_mount["source"] == "./config.yaml"
    assert config_mount["read_only"] is True
    assert config_mount["bind"]["create_host_path"] is False

    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in dockerignore
    assert ".env.*" in dockerignore
    assert "config.yaml" in dockerignore


def test_application_container_has_healthcheck() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "HEALTHCHECK" in dockerfile
    assert "dischat.healthcheck" in dockerfile
    assert "uv sync --locked --no-dev" in dockerfile


def test_live_e2e_uses_test_image_with_development_dependencies() -> None:
    live_compose = yaml.safe_load(
        (REPO_ROOT / "docker-compose.live-e2e.yml").read_text(encoding="utf-8")
    )
    assert live_compose["services"]["tests"]["build"]["target"] == "test"
