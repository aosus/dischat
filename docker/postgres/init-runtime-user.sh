#!/bin/sh
set -eu

if [ -z "${DISCHAT_DB_PASSWORD:-}" ]; then
    echo "DISCHAT_DB_PASSWORD is required" >&2
    exit 1
fi

psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set=runtime_password="$DISCHAT_DB_PASSWORD" <<-'SQL'
	CREATE ROLE dischat
	    LOGIN
	    PASSWORD :'runtime_password'
	    NOSUPERUSER
	    NOCREATEDB
	    NOCREATEROLE
	    NOREPLICATION;
	ALTER DATABASE dischat OWNER TO dischat;
	ALTER SCHEMA public OWNER TO dischat;
SQL
