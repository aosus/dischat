#!/bin/sh
set -eu

if [ -z "${POSTGRES_ADMIN_PASSWORD:-}" ] || [ -z "${DISCHAT_DB_PASSWORD:-}" ]; then
    echo "POSTGRES_ADMIN_PASSWORD and DISCHAT_DB_PASSWORD are required" >&2
    exit 1
fi

admin_psql() {
    PGPASSWORD="$POSTGRES_ADMIN_PASSWORD" psql \
        --set=ON_ERROR_STOP=1 --username dischat_admin "$@"
}

legacy_psql() {
    PGPASSWORD="${DISCHAT_LEGACY_DB_PASSWORD:-$DISCHAT_DB_PASSWORD}" psql \
        --set=ON_ERROR_STOP=1 --username dischat "$@"
}

# New volumes already contain dischat_admin. Older Compose deployments used
# `dischat` as the bootstrap superuser, so promote/create the dedicated admin
# through that legacy account before removing its elevated privileges.
if ! admin_psql --quiet --tuples-only --command "SELECT 1" >/dev/null 2>&1; then
    if ! legacy_psql --quiet --tuples-only --command "SELECT 1" >/dev/null 2>&1; then
        echo "cannot authenticate as dischat_admin or legacy dischat owner" >&2
        exit 1
    fi
    if [ "$(legacy_psql --quiet --tuples-only --no-align --command \
        "SELECT COUNT(*) FROM pg_roles WHERE rolname = 'dischat_admin'")" = "0" ]; then
        legacy_psql --set=admin_password="$POSTGRES_ADMIN_PASSWORD" --command \
            "CREATE ROLE dischat_admin LOGIN SUPERUSER PASSWORD :'admin_password'"
    else
        legacy_psql --set=admin_password="$POSTGRES_ADMIN_PASSWORD" --command \
            "ALTER ROLE dischat_admin LOGIN SUPERUSER PASSWORD :'admin_password'"
    fi
fi

if [ "$(admin_psql --quiet --tuples-only --no-align --command \
    "SELECT COUNT(*) FROM pg_roles WHERE rolname = 'dischat'")" = "0" ]; then
    admin_psql --set=runtime_password="$DISCHAT_DB_PASSWORD" --command \
        "CREATE ROLE dischat LOGIN PASSWORD :'runtime_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
else
    admin_psql --set=runtime_password="$DISCHAT_DB_PASSWORD" --command \
        "ALTER ROLE dischat LOGIN PASSWORD :'runtime_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
fi

admin_psql --command "ALTER DATABASE dischat OWNER TO dischat"
admin_psql --command "ALTER SCHEMA public OWNER TO dischat"
