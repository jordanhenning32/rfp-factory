#!/bin/sh
set -e

# Run migrations only when starting the app, not the worker.
if [ "$1" = "app" ]; then
    echo "[entrypoint] running alembic upgrade head"
    alembic upgrade head
    echo "[entrypoint] starting app"
    exec python -m app.main
elif [ "$1" = "worker" ]; then
    echo "[entrypoint] starting worker"
    exec python -m app.jobs.worker
else
    exec "$@"
fi
