"""
scheduler.py
Background jobs using APScheduler:
- Every 10 min  : fetch events from all active devices
- Every 1h 5min : auto-create user records from new event employee IDs
- Every 6 hours : push all DB user records to every device (cross-device user sync)
- Daily 03:00   : pull faces from all devices → push to all devices (full face sync)
- Daily 09:00   : send previous day's attendance email report
- Weekly Mon 06:00: send weekly attendance email report
"""
import os
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

log = logging.getLogger("scheduler")
_scheduler = None


def _job_fetch_all():
    try:
        from services.collector import fetch_all_active_devices
        results = fetch_all_active_devices()
        total = sum(v["count"] for v in results.values())
        log.info(f"[HOURLY FETCH] {len(results)} devices, {total} new events")
    except Exception as e:
        log.error(f"[HOURLY FETCH] error: {e}")


def _job_sync_new_users():
    try:
        from services.collector import sync_new_users_from_events
        n = sync_new_users_from_events()
        if n:
            log.info(f"[USER SYNC] {n} new users created from events")
    except Exception as e:
        log.error(f"[USER SYNC] error: {e}")



def _job_sync_users_from_devices():
    """Pull users from device UserInfo and auto-create any missing in DB."""
    try:
        from services.collector import sync_users_from_devices
        summary = sync_users_from_devices()
        added = sum(v["added"] for v in summary.values())
        log.info(f"[USER PULL] auto-created {added} users from devices")
    except Exception as e:
        log.error(f"[USER PULL] error: {e}")


def _job_cross_device_sync():
    """Push user records to all devices so users can check in on any device."""
    try:
        from services.collector import sync_users_across_devices
        summary = sync_users_across_devices()
        pushed = sum(v["pushed"] for v in summary.values())
        log.info(f"[DEVICE SYNC] pushed {pushed} user records across {len(summary)} devices")
    except Exception as e:
        log.error(f"[DEVICE SYNC] error: {e}")


def _job_full_face_sync():
    """
    Daily face sync:
    1. Pull any face images stored on each device → local disk cache
    2. Push all locally-cached faces → every device
    Ensures every device has every enrolled employee's face.
    """
    try:
        from services.face_sync import full_sync_faces
        result = full_sync_faces()
        log.info(f"[FACE SYNC] pulled={result['total_pulled']} pushed={result['total_pushed']}")
    except Exception as e:
        log.error(f"[FACE SYNC] error: {e}")


def _job_weekly_email():
    try:
        from services.email_report import send_weekly_report
        send_weekly_report()
        log.info("[WEEKLY EMAIL] sent")
    except Exception as e:
        log.error(f"[WEEKLY EMAIL] error: {e}")


def _job_daily_email():
    try:
        from services.email_report import send_daily_report
        sent = send_daily_report()
        log.info(f"[DAILY EMAIL] {'sent' if sent else 'skipped (disabled or SMTP not configured)'}")
    except Exception as e:
        log.error(f"[DAILY EMAIL] error: {e}")


def start_scheduler(app):
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    tz = os.getenv("SCHEDULER_TZ", "America/Mexico_City")
    _scheduler = BackgroundScheduler(timezone=tz, daemon=True)

    # Event fetch every 10 minutes (was hourly) — so punches appear within
    # ~10 min instead of up to an hour. Window is 24h wide + dedup, so frequent
    # overlapping fetches are safe and free of duplicates.
    _scheduler.add_job(_job_fetch_all, IntervalTrigger(minutes=10),
                       id="fetch_all", name="Device event fetch (10 min)",
                       replace_existing=True, misfire_grace_time=300)

    # Auto-create users 5 min after each fetch
    _scheduler.add_job(_job_sync_new_users, IntervalTrigger(hours=1, minutes=5),
                       id="sync_users", name="Sync new users from events",
                       replace_existing=True, misfire_grace_time=300)

    # Cross-device user push every 6 hours
    _scheduler.add_job(_job_cross_device_sync, IntervalTrigger(hours=6),
                       id="cross_device_sync", name="Push users to all devices",
                       replace_existing=True, misfire_grace_time=600)

    # Pull users from devices every 6 hours (auto-creates users enrolled on device)
    _scheduler.add_job(_job_sync_users_from_devices, IntervalTrigger(hours=6, minutes=30),
                       id="sync_users_from_devices", name="Pull users from devices",
                       replace_existing=True, misfire_grace_time=600)

    # Full face sync daily at 03:00
    _scheduler.add_job(_job_full_face_sync, CronTrigger(hour=3, minute=0),
                       id="face_sync", name="Daily pull+push face sync",
                       replace_existing=True)

    # Weekly email Monday 06:00
    _scheduler.add_job(_job_weekly_email, CronTrigger(day_of_week="mon", hour=6, minute=0),
                       id="weekly_email", name="Weekly email report",
                       replace_existing=True)

    # Daily email 09:00 — previous day's attendance report
    _scheduler.add_job(_job_daily_email, CronTrigger(hour=9, minute=0),
                       id="daily_email", name="Daily email report (09:00)",
                       replace_existing=True)

    _scheduler.start()
    log.info("Scheduler started — 10-min fetch | 6h user sync | daily face sync | daily 09:00 email | weekly email")
    return _scheduler


def get_scheduler():
    return _scheduler
