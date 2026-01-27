"""Celery tasks for the dialer orchestrator."""

import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from celery_app import celery_app
from dispatch_agent import dispatch_agent_for_lead, get_or_create_sip_trunk
from models import Batch, CallLog, Lead
from settings import settings

logger = logging.getLogger(__name__)


def get_async_session_factory():
    """Create a fresh async engine and session factory for each task.
    
    Uses NullPool to avoid connection leaks - each query opens and closes
    a connection immediately. This is necessary because Celery workers
    can exhaust connection limits with pooled connections.
    """
    engine = create_async_engine(
        settings.DATABASE_URL,
        poolclass=NullPool,  # No connection pooling - prevents exhaustion
    )
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    ), engine


async def _run_with_session(coro_factory):
    """Run an async function with a session and properly dispose the engine."""
    SessionLocal, engine = get_async_session_factory()
    try:
        async with SessionLocal() as session:
            return await coro_factory(session)
    finally:
        await engine.dispose()


def run_async(coro):
    """Helper to run async code in sync Celery tasks."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="tasks.refill_all_active_batches")
def refill_all_active_batches():
    """
    Periodic task that runs every 10s.
    Queries all active batches and triggers refill_capacity for each.
    """
    return run_async(_refill_all_active_batches())


async def _refill_all_active_batches():
    """Async implementation of refill_all_active_batches."""
    async def _impl(session):
        result = await session.execute(
            select(Batch.batch_name).where(Batch.is_active.is_(True))
        )
        active_batches = result.scalars().all()

        for batch_name in active_batches:
            refill_capacity.delay(batch_name)
            logger.debug(f"Triggered refill_capacity for batch: {batch_name}")

        return {"active_batches": len(active_batches)}
    
    return await _run_with_session(_impl)


@celery_app.task(name="tasks.refill_capacity")
def refill_capacity(batch_name: str):
    """
    Check current inflight count for a batch and dispatch more leads if needed.
    """
    return run_async(_refill_capacity(batch_name))


async def _refill_capacity(batch_name: str):
    """Async implementation of refill_capacity."""
    async def _impl(session):
        # Get batch config
        batch_result = await session.execute(
            select(Batch).where(Batch.batch_name == batch_name)
        )
        batch = batch_result.scalar_one_or_none()

        if not batch or not batch.is_active:
            logger.info(f"Batch {batch_name} not found or not active")
            return {"dispatched": 0}

        # Count current inflight leads
        inflight_result = await session.execute(
            select(Lead)
            .where(Lead.batch_name == batch_name, Lead.inflight.is_(True))
        )
        inflight_count = len(inflight_result.scalars().all())

        logger.debug(
            f"Batch {batch_name}: inflight={inflight_count}, "
            f"threshold={batch.refill_threshold}, max={batch.max_parallel_calls}"
        )

        # If below threshold, dispatch more
        if inflight_count < batch.refill_threshold:
            slots = batch.max_parallel_calls - inflight_count
            dispatched = 0

            for _ in range(slots):
                dispatch_next_lead.delay(batch_name)
                dispatched += 1

            logger.info(f"Batch {batch_name}: dispatched {dispatched} new calls")
            return {"dispatched": dispatched, "inflight": inflight_count}

        return {"dispatched": 0, "inflight": inflight_count}
    
    return await _run_with_session(_impl)


@celery_app.task(name="tasks.dispatch_next_lead")
def dispatch_next_lead(batch_name: str):
    """
    Atomically claim and dispatch the next eligible lead.
    Uses FOR UPDATE SKIP LOCKED for concurrency safety.
    """
    return run_async(_dispatch_next_lead(batch_name))


async def _dispatch_next_lead(batch_name: str):
    """Async implementation of dispatch_next_lead."""
    SessionLocal, engine = get_async_session_factory()
    try:
        async with SessionLocal() as session:
            # Get batch config
            batch_result = await session.execute(
                select(Batch).where(Batch.batch_name == batch_name)
            )
            batch = batch_result.scalar_one_or_none()

            if not batch or not batch.is_active:
                return {"status": "batch_inactive"}

            today = date.today()

            # Build eligibility query with priority ordering:
            # 1. Recall leads (status=0, redial_date<=today, ready_for_call=true)
            # 2. Fresh leads (status IS NULL)
            # Then by attempt_count ASC, row_order ASC
            eligible_query = (
                select(Lead)
                .where(
                    Lead.batch_name == batch_name,
                    Lead.inflight.is_(False),
                    Lead.attempt_count < batch.max_attempts,
                    or_(
                        Lead.status.is_(None),
                        and_(
                            Lead.status == 0,
                            Lead.redial_date <= today,
                            Lead.ready_for_call.is_(True),
                        ),
                    ),
                )
                .order_by(
                    # Priority: recalls first (status=0), then fresh (status=NULL)
                    Lead.status.desc().nulls_last(),
                    Lead.attempt_count.asc(),
                    Lead.row_order.asc(),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )

            result = await session.execute(eligible_query)
            lead = result.scalar_one_or_none()

            if not lead:
                logger.debug(f"No eligible leads for batch {batch_name}")
                return {"status": "no_leads"}

            # Generate room name
            room_name = f"dialer-{batch_name}-{lead.id}-{uuid.uuid4().hex[:8]}"

            # Mark as inflight
            lead.inflight = True
            lead.inflight_room_id = room_name
            lead.last_call_at = datetime.utcnow()

            # Create call log entry
            call_log = CallLog(
                lead_id=lead.id,
                room_id=room_name,
            )
            session.add(call_log)

            await session.commit()

            logger.info(f"Claimed lead {lead.id} for batch {batch_name}, room={room_name}")

            # Dispatch to LiveKit
            try:
                # Get or create SIP trunk before dispatching
                trunk_id = await get_or_create_sip_trunk()
                if not trunk_id:
                    logger.error(f"No SIP trunk available for lead {lead.id}")
                    lead.inflight = False
                    lead.inflight_room_id = None
                    await session.commit()
                    return {"status": "no_trunk", "error": "SIP trunk not available"}
                
                metadata = {
                    "lead_id": str(lead.id),
                    "batch_name": batch_name,
                    "phone": lead.phone,
                    "name": lead.name,
                    "attempt_count": lead.attempt_count,
                    "lead_metadata": lead.extra_data,
                    "sip_trunk_id": trunk_id,
                }

                await dispatch_agent_for_lead(room_name=room_name, metadata=metadata)
                logger.info(f"Dispatched agent for lead {lead.id} with trunk {trunk_id}")

                return {
                    "status": "dispatched",
                    "lead_id": str(lead.id),
                    "room_name": room_name,
                }

            except Exception as e:
                logger.error(f"Failed to dispatch agent for lead {lead.id}: {e}")
                # Clear inflight on failure
                lead.inflight = False
                lead.inflight_room_id = None
                await session.commit()
                return {"status": "dispatch_failed", "error": str(e)}
    finally:
        await engine.dispose()


@celery_app.task(name="tasks.reconcile_stuck_inflight")
def reconcile_stuck_inflight():
    """
    Periodic task to clear inflight status for leads stuck too long.
    Runs every 10 minutes.
    """
    return run_async(_reconcile_stuck_inflight())


async def _reconcile_stuck_inflight():
    """Async implementation of reconcile_stuck_inflight."""
    async def _impl(session):
        timeout = datetime.utcnow() - timedelta(
            minutes=settings.INFLIGHT_TIMEOUT_MINUTES
        )

        # Find and clear stuck leads
        result = await session.execute(
            update(Lead)
            .where(
                Lead.inflight.is_(True),
                Lead.last_call_at < timeout,
            )
            .values(
                inflight=False,
                inflight_room_id=None,
            )
            .returning(Lead.id)
        )

        cleared_ids = result.scalars().all()
        await session.commit()

        if cleared_ids:
            logger.warning(f"Cleared {len(cleared_ids)} stuck inflight leads")

        return {"cleared": len(cleared_ids)}
    
    return await _run_with_session(_impl)


@celery_app.task(name="tasks.make_recalls_ready")
def make_recalls_ready():
    """
    Periodic task to mark recall leads as ready when their redial_date arrives.
    Runs every hour.
    """
    return run_async(_make_recalls_ready())


async def _make_recalls_ready():
    """Async implementation of make_recalls_ready."""
    async def _impl(session):
        today = date.today()

        result = await session.execute(
            update(Lead)
            .where(
                Lead.status == 0,
                Lead.redial_date <= today,
                Lead.ready_for_call.is_(False),
            )
            .values(ready_for_call=True)
            .returning(Lead.id)
        )

        updated_ids = result.scalars().all()
        await session.commit()

        if updated_ids:
            logger.info(f"Made {len(updated_ids)} recall leads ready")

        return {"updated": len(updated_ids)}
    
    return await _run_with_session(_impl)
