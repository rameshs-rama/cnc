"""Standalone compute worker entry point.

Runs the same handlers as the in-process workers, for deployments that scale
reconstruction, CAM and simulation separately from the API (PRD 7.2).
"""

from __future__ import annotations

import logging
import signal
import time

from app.config import get_settings
from app.db import create_all
from app.jobs.worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("mip.worker")


def main() -> None:
    settings = get_settings()
    create_all()
    workers = [Worker(name=f"mip-worker-{i}") for i in range(max(1, settings.worker_threads))]
    for worker in workers:
        worker.start()
    logger.info("compute worker started with %d thread(s)", len(workers))

    stopping = False

    def handle(signum, _frame):
        nonlocal stopping
        logger.info("signal %s received; finishing the current job then stopping", signum)
        stopping = True
        for worker in workers:
            worker.stop()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    while not stopping:
        time.sleep(1.0)
    for worker in workers:
        worker.join(timeout=30)
    logger.info("compute worker stopped")


if __name__ == "__main__":
    main()
