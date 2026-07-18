"""RQ worker entrypoint.

Runs in the worker container of docker-compose. Picks up proposal-run jobs
from the default queue once the agent pipeline is wired up (Weeks 3+).
"""

from __future__ import annotations

import logging

from redis import Redis
from rq import Queue, Worker

from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rfp.worker")


def main() -> None:
    settings = get_settings()
    log.info("Starting RQ worker against %s", settings.redis_url)

    # RQ 2.x removed the `Connection` context-manager; pass connection
    # explicitly to Queue and Worker instead.
    redis_conn = Redis.from_url(settings.redis_url)
    queue = Queue("default", connection=redis_conn)
    worker = Worker([queue], connection=redis_conn)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
