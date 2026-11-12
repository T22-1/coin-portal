#!/usr/bin/env bash
set -euo pipefail

python manage.py reconcile_migration_history
python manage.py migrate --noinput
python manage.py createsuperuser --noinput || true
exec gunicorn coinportal.wsgi:application   --bind "0.0.0.0:${PORT:-8000}"   --timeout "${GUNICORN_TIMEOUT:-120}"
