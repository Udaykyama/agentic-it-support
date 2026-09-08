import argparse
import signal
import socket
import threading

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.jobs import JobProcessor, claim_next


def heartbeat_key():
    return f"neuraldesk:worker:{socket.gethostname()}"


def main():
    parser = argparse.ArgumentParser(description="Process durable NeuralDesk jobs")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--once", action="store_true", help="Process at most one available job")
    args = parser.parse_args()
    from api import create_app

    app = create_app()
    settings = app.config["SETTINGS"]
    redis, database = app.extensions["redis"], app.extensions["database"]
    logger = app.extensions["logger"]
    if args.healthcheck:
        return 0 if redis.exists(heartbeat_key()) else 1
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    processor = JobProcessor(database, settings, app.extensions["ai"], logger)
    logger.info("worker_started")
    try:
        while not stop.is_set():
            redis.set(heartbeat_key(), "ready", ex=settings.job_lease_seconds)
            job = claim_next(database, settings)
            if job:
                processor.process(job)
            if args.once:
                break
            if job is None:
                stop.wait(settings.worker_poll_seconds)
    except (RedisError, SQLAlchemyError):
        logger.exception("worker_dependency_failed")
        return 1
    finally:
        database.engine.dispose()
        redis.close()
    logger.info("worker_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
