import json
import logging
import socket
import time
import uuid
from datetime import timedelta

from sqlalchemy import select

from .acquisition import (
    mark_acquisition_failed,
    reconcile_importing,
    run_acquisition_download,
    run_acquisition_monitor,
    run_acquisition_search,
)
from .config import get_settings
from .db import SessionLocal, initialise_database
from .discovery import refresh_openlibrary_discovery
from .jobs import claim, complete, enqueue, recover_stale, retry_or_fail
from .library import group_logical_books, reconcile_sidecars, scan_library
from .metadata import auto_scrape_book, enrich_pending, refresh_book
from .models import AppSetting, AuditEvent, Book, Job, JobStatus, ReviewState, now

log = logging.getLogger("digest.worker")


def run_once() -> dict[str, int]:
    with SessionLocal() as db:
        marker = db.get(AppSetting, "initial_scan_complete")
        stats = scan_library(db, initial=marker is None)
        if marker is None:
            db.add(AppSetting(key="initial_scan_complete", value="true"))
            db.commit()
        sidecar_marker = db.get(AppSetting, "baseline_sidecars_imported_v1")
        if sidecar_marker is None:
            language = db.get(AppSetting, "default_language")
            stats["sidecars"] = reconcile_sidecars(db, language.value if language else "en")
            db.add(AppSetting(key="baseline_sidecars_imported_v1", value="true"))
            db.commit()
        else:
            stats["sidecars"] = 0
        stats["grouped"] = group_logical_books(db)
        stats["enriched"] = enrich_pending(db)
        stats["acquired"] = reconcile_importing(db)
        return stats


def run_metadata_refresh(db, job: Job) -> None:
    payload = json.loads(job.payload_json or "{}")
    book_id = payload.get("book_id")
    if book_id:
        book = db.get(Book, book_id)
        if book is not None:
            refresh_book(db, book)
        return
    for book in db.scalars(select(Book).where(Book.review_state == ReviewState.READY)).all():
        refresh_book(db, book)


def run_metadata_auto_scrape(db, job: Job) -> None:
    book_id = json.loads(job.payload_json or "{}").get("book_id")
    book = db.get(Book, book_id) if book_id else None
    if book is not None:
        auto_scrape_book(db, book)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    initialise_database()
    settings = get_settings()
    worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        recovered = recover_stale(db)
        if recovered:
            log.warning("recovered %s stale jobs", recovered)
        active_scan = db.scalar(
            select(Job.id).where(
                Job.kind == "library_scan",
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        )
        if active_scan is None:
            enqueue(db, "library_scan")
        active_refresh = db.scalar(
            select(Job.id).where(
                Job.kind == "metadata_refresh_all",
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        )
        if active_refresh is None:
            config = db.get(AppSetting, "metadata_refresh_hours")
            hours = int(config.value) if config else settings.metadata_refresh_hours
            enqueue(db, "metadata_refresh_all", run_after=now() + timedelta(hours=hours))
        active_discovery = db.scalar(
            select(Job.id).where(
                Job.kind == "discovery_refresh",
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        )
        if active_discovery is None:
            enqueue(db, "discovery_refresh")
    while True:
        with SessionLocal() as db:
            job = claim(db, worker_id)
            if job is None:
                time.sleep(2)
                continue
            try:
                if job.kind == "metadata_refresh":
                    run_metadata_refresh(db, job)
                elif job.kind == "metadata_auto_scrape":
                    run_metadata_auto_scrape(db, job)
                elif job.kind == "acquisition_search":
                    run_acquisition_search(db, job)
                elif job.kind == "acquisition_download":
                    run_acquisition_download(db, job)
                elif job.kind == "acquisition_monitor":
                    run_acquisition_monitor(db, job)
                elif job.kind == "metadata_refresh_all":
                    run_metadata_refresh(db, job)
                    config = db.get(AppSetting, "metadata_refresh_hours")
                    hours = int(config.value) if config else settings.metadata_refresh_hours
                    enqueue(db, "metadata_refresh_all", run_after=now() + timedelta(hours=hours))
                elif job.kind == "discovery_refresh":
                    count = refresh_openlibrary_discovery(db)
                    log.info("discovery refresh complete: %s cached items", count)
                    config = db.get(AppSetting, "discovery_refresh_hours")
                    hours = int(config.value) if config else 24
                    enqueue(db, "discovery_refresh", run_after=now() + timedelta(hours=hours))
                elif job.kind != "library_scan":
                    raise ValueError(f"Unknown job kind: {job.kind}")
                else:
                    log.info("library scan complete: %s", run_once())
                complete(db, job)
                if job.kind == "library_scan":
                    enqueue(
                        db,
                        "library_scan",
                        run_after=now()
                        + timedelta(seconds=max(settings.scan_interval_seconds, 10)),
                    )
            except Exception as exc:
                log.exception("job %s failed", job.id)
                db.add(
                    AuditEvent(
                        level="error", event="worker_error", message=f"{type(exc).__name__}: {exc}"
                    )
                )
                db.commit()
                retry_or_fail(db, job, exc)
                if job.status == JobStatus.FAILED:
                    if job.kind.startswith("acquisition_"):
                        mark_acquisition_failed(db, job, exc)
                    if job.kind == "library_scan":
                        enqueue(
                            db,
                            "library_scan",
                            run_after=now()
                            + timedelta(seconds=max(settings.scan_interval_seconds, 10)),
                        )
                    elif job.kind == "metadata_refresh_all":
                        config = db.get(AppSetting, "metadata_refresh_hours")
                        hours = int(config.value) if config else settings.metadata_refresh_hours
                        enqueue(
                            db,
                            "metadata_refresh_all",
                            run_after=now() + timedelta(hours=hours),
                        )
                    elif job.kind == "discovery_refresh":
                        config = db.get(AppSetting, "discovery_refresh_hours")
                        hours = int(config.value) if config else 24
                        enqueue(
                            db,
                            "discovery_refresh",
                            run_after=now() + timedelta(hours=hours),
                        )


if __name__ == "__main__":
    main()
