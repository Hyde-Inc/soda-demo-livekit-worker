"""Celery configuration for the dialer orchestrator."""

from celery import Celery

from settings import settings

celery_app = Celery("dialer")

celery_app.conf.update(
    broker_url=settings.celery_broker,
    result_backend=settings.celery_backend,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    timezone=settings.TIMEZONE,
    enable_utc=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_time_limit=300,  # 5 minutes max per task
    worker_max_tasks_per_child=100,  # Restart worker after 100 tasks
    imports=["tasks"],  # Import tasks module
    # Disable features that require Redis pub/sub (needs &* ACL permission)
    worker_enable_remote_control=False,
    broker_transport_options={
        "fanout_prefix": True,
        "fanout_patterns": True,
        "visibility_timeout": 3600,
    },
)

# Beat schedule for periodic tasks
celery_app.conf.beat_schedule = {
    "refill-all-active-batches": {
        "task": "tasks.refill_all_active_batches",
        "schedule": 10.0,  # every 10 seconds
    },
    "make-recalls-ready": {
        "task": "tasks.make_recalls_ready",
        "schedule": 3600.0,  # every 60 minutes
    },
    "reconcile-stuck-inflight": {
        "task": "tasks.reconcile_stuck_inflight",
        "schedule": 600.0,  # every 10 minutes
    },
}
