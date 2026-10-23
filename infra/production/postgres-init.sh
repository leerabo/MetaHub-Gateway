#!/bin/sh
set -eu
# Secrets stay out of argv and SQL logs; generated passwords contain only hex.
app_password=$(cat /run/secrets/app-password)
printf "CREATE ROLE metahub LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '%s';\nALTER DATABASE metahub OWNER TO metahub;\nGRANT CREATE ON SCHEMA public TO metahub;\n" "$app_password" | psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --no-psqlrc --set ON_ERROR_STOP=1 >/dev/null
