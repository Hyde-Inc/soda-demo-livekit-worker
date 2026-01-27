"""FastAPI server for dialer orchestration."""

import csv
import io
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from livekit import api
from livekit.protocol.sip import CreateSIPOutboundTrunkRequest, SIPOutboundTrunkInfo

from db import get_session
from models import Batch, CallLog, Lead
from schema import (
    BatchNameAvailabilityResponse,
    BatchNamesResponse,
    BatchResetResponse,
    BatchResponse,
    BatchStartResponse,
    BatchStopResponse,
    CallLogResponse,
    LeadResponse,
    StatsResponse,
    UploadResponse,
)
from settings import settings

# Load environment variables
load_dotenv(dotenv_path=".env.local")

logger = logging.getLogger(__name__)

# --- JWT Configuration ---
# Generate with: openssl rand -hex 32
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Fake users database (replace with real DB in production)
fake_users_db = {
    "admin": {
        "username": "admin",
        "full_name": "Admin User",
        "email": "admin@example.com",
        "hashed_password": "$argon2id$v=19$m=65536,t=3,p=4$RajXyHBmlrZLxu0nsQcjEQ$aMa2MVHW5nk4aym6T4So40mgyC/7uN5FpTk5fpwjd/Y",
        "disabled": False,
    }
}


# --- Auth Models ---
class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: str | None = None


class User(BaseModel):
    username: str
    email: str | None = None
    full_name: str | None = None
    disabled: bool | None = None


class UserInDB(User):
    hashed_password: str


# --- Auth Utilities ---
password_hash = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return password_hash.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return password_hash.hash(password)


def get_user(db: dict, username: str) -> UserInDB | None:
    if username in db:
        user_dict = db[username]
        return UserInDB(**user_dict)
    return None


def authenticate_user(fake_db: dict, username: str, password: str) -> UserInDB | bool:
    user = get_user(fake_db, username)
    if not user:
        return False
    if not verify_password(password, user.hashed_password):
        return False
    return user


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except InvalidTokenError:
        raise credentials_exception
    user = get_user(fake_users_db, username=token_data.username)
    if user is None:
        raise credentials_exception
    return user


async def get_current_active_user(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    if current_user.disabled:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user


app = FastAPI(title="Dialer Orchestrator API", version="1.0.0")


# --- LiveKit API Helper ---
def get_livekit_api() -> api.LiveKitAPI:
    """Get LiveKit API client with proper URL conversion."""
    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    # Convert ws:// to http:// for API calls
    if url and url.startswith("ws://"):
        url = url.replace("ws://", "http://")
    elif url and url.startswith("wss://"):
        url = url.replace("wss://", "https://")

    return api.LiveKitAPI(url=url, api_key=api_key, api_secret=api_secret)


# --- SIP Trunk Models ---
class SIPTrunkRequest(BaseModel):
    """Request model for creating SIP outbound trunk."""
    name: str = "My trunk"
    address: str
    numbers: list[str]
    auth_username: str
    auth_password: str
    destination_country: str = "INDIA"
    transport: str = "SIP_TRANSPORT_TCP"


class SIPTrunkResponse(BaseModel):
    """Response model for SIP trunk creation."""
    success: bool
    trunk_id: Optional[str] = None
    message: str


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/token", response_model=Token)
async def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> Token:
    """
    OAuth2 compatible token login, get an access token for future requests.
    
    Default credentials: username=admin, password=secret
    """
    user = authenticate_user(fake_users_db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return Token(access_token=access_token, token_type="bearer")


@app.post("/upload_csv", response_model=UploadResponse)
async def upload_csv(
    batch_name: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    session: AsyncSession = Depends(get_session),
):
    """
    Upload a CSV file of leads for a batch.

    - Creates batch if not exists (with default settings)
    - Parses CSV rows and inserts leads with row_order
    - Dedupes by (batch_name, phone) - skips duplicates
    """
    # Read file content
    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    # Create or get batch
    batch_result = await session.execute(
        select(Batch).where(Batch.batch_name == batch_name)
    )
    batch = batch_result.scalar_one_or_none()

    if not batch:
        batch = Batch(
            batch_name=batch_name,
            max_parallel_calls=settings.DEFAULT_MAX_PARALLEL_CALLS,
            refill_threshold=settings.DEFAULT_REFILL_THRESHOLD,
            max_attempts=settings.DEFAULT_MAX_ATTEMPTS,
        )
        session.add(batch)
        await session.flush()

    # Parse CSV
    reader = csv.DictReader(io.StringIO(text))

    # Get current max row_order for this batch
    max_order_result = await session.execute(
        select(func.coalesce(func.max(Lead.row_order), 0)).where(
            Lead.batch_name == batch_name
        )
    )
    current_max_order = max_order_result.scalar() or 0

    inserted_count = 0
    skipped_count = 0
    row_order = current_max_order

    for row in reader:
        # Expect at minimum 'phone' column
        phone = row.get("phone") or row.get("Phone") or row.get("PHONE")
        if not phone:
            skipped_count += 1
            continue

        phone = phone.strip()
        if not phone:
            skipped_count += 1
            continue

        name = row.get("name") or row.get("Name") or row.get("NAME")

        # Build extra_data from remaining columns
        extra_data = {k: v for k, v in row.items() if k.lower() not in ("phone", "name")}

        row_order += 1

        # Use INSERT ... ON CONFLICT DO NOTHING for deduplication
        stmt = (
            insert(Lead)
            .values(
                batch_name=batch_name,
                row_order=row_order,
                phone=phone,
                name=name.strip() if name else None,
                extra_data=extra_data if extra_data else None,
            )
            .on_conflict_do_nothing(constraint="uq_batch_phone")
        )

        result = await session.execute(stmt)
        if result.rowcount > 0:
            inserted_count += 1
        else:
            skipped_count += 1

    await session.commit()

    return UploadResponse(
        batch_name=batch_name,
        inserted_count=inserted_count,
        skipped_count=skipped_count,
        message=f"Uploaded {inserted_count} leads, skipped {skipped_count} duplicates/invalid",
    )


@app.post("/batches/{batch_name}/start", response_model=BatchStartResponse)
async def start_batch(
    batch_name: str,
    session: AsyncSession = Depends(get_session),
):
    """
    Start a batch - sets is_active=true and triggers refill task.
    """
    # Get batch
    result = await session.execute(
        select(Batch).where(Batch.batch_name == batch_name)
    )
    batch = result.scalar_one_or_none()

    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_name}' not found")

    if batch.is_active:
        return BatchStartResponse(
            batch_name=batch_name,
            started=False,
            message="Batch is already active",
        )

    # Set active
    batch.is_active = True
    await session.commit()

    # Trigger Celery task
    try:
        from tasks import refill_capacity

        refill_capacity.delay(batch_name)
        logger.info(f"Triggered refill_capacity for batch: {batch_name}")
    except Exception as e:
        logger.warning(f"Could not trigger Celery task: {e}")

    return BatchStartResponse(
        batch_name=batch_name,
        started=True,
        message="Batch started successfully",
    )


@app.post("/batches/{batch_name}/stop", response_model=BatchStopResponse)
async def stop_batch(
    batch_name: str,
    session: AsyncSession = Depends(get_session),
):
    """
    Stop a batch - sets is_active=false.
    """
    result = await session.execute(
        select(Batch).where(Batch.batch_name == batch_name)
    )
    batch = result.scalar_one_or_none()

    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_name}' not found")

    if not batch.is_active:
        return BatchStopResponse(
            batch_name=batch_name,
            stopped=False,
            message="Batch is already stopped",
        )

    batch.is_active = False
    await session.commit()

    return BatchStopResponse(
        batch_name=batch_name,
        stopped=True,
        message="Batch stopped successfully",
    )


@app.get("/batches/{batch_name}", response_model=BatchResponse)
async def get_batch(
    batch_name: str,
    session: AsyncSession = Depends(get_session),
):
    """Get batch details."""
    result = await session.execute(
        select(Batch).where(Batch.batch_name == batch_name)
    )
    batch = result.scalar_one_or_none()

    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_name}' not found")

    return batch


@app.get("/batches/{batch_name}/stats", response_model=StatsResponse)
async def get_batch_stats(
    batch_name: str,
    session: AsyncSession = Depends(get_session),
):
    """
    Get statistics for a batch.
    """
    from datetime import date

    # Get batch
    batch_result = await session.execute(
        select(Batch).where(Batch.batch_name == batch_name)
    )
    batch = batch_result.scalar_one_or_none()

    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_name}' not found")

    # Total leads
    total_result = await session.execute(
        select(func.count(Lead.id)).where(Lead.batch_name == batch_name)
    )
    total_leads = total_result.scalar() or 0

    # Not called (status IS NULL)
    not_called_result = await session.execute(
        select(func.count(Lead.id)).where(
            Lead.batch_name == batch_name, Lead.status.is_(None)
        )
    )
    not_called = not_called_result.scalar() or 0

    # Success (status = 1)
    success_result = await session.execute(
        select(func.count(Lead.id)).where(
            Lead.batch_name == batch_name, Lead.status == 1
        )
    )
    success = success_result.scalar() or 0

    # Not interested (status = -1)
    not_interested_result = await session.execute(
        select(func.count(Lead.id)).where(
            Lead.batch_name == batch_name, Lead.status == -1
        )
    )
    not_interested = not_interested_result.scalar() or 0

    # Recall pending (status = 0)
    recall_result = await session.execute(
        select(func.count(Lead.id)).where(
            Lead.batch_name == batch_name, Lead.status == 0
        )
    )
    recall_pending = recall_result.scalar() or 0

    # Inflight
    inflight_result = await session.execute(
        select(func.count(Lead.id)).where(
            Lead.batch_name == batch_name, Lead.inflight.is_(True)
        )
    )
    inflight = inflight_result.scalar() or 0

    # Eligible now (ready to call)
    today = date.today()
    eligible_result = await session.execute(
        select(func.count(Lead.id)).where(
            Lead.batch_name == batch_name,
            Lead.inflight.is_(False),
            Lead.attempt_count < batch.max_attempts,
            (
                Lead.status.is_(None)
                | (
                    (Lead.status == 0)
                    & (Lead.redial_date <= today)
                    & (Lead.ready_for_call.is_(True))
                )
            ),
        )
    )
    eligible_now = eligible_result.scalar() or 0

    # Max attempts reached
    max_attempts_result = await session.execute(
        select(func.count(Lead.id)).where(
            Lead.batch_name == batch_name,
            Lead.attempt_count >= batch.max_attempts,
        )
    )
    max_attempts_reached = max_attempts_result.scalar() or 0

    return StatsResponse(
        batch_name=batch_name,
        is_active=batch.is_active,
        total_leads=total_leads,
        not_called=not_called,
        success=success,
        not_interested=not_interested,
        recall_pending=recall_pending,
        inflight=inflight,
        eligible_now=eligible_now,
        max_attempts_reached=max_attempts_reached,
    )


@app.get("/batches", response_model=BatchNamesResponse)
async def get_batch_names(
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: AsyncSession = Depends(get_session),
):
    """
    Get all batch names from the database.
    
    Requires JWT authentication.
    """
    result = await session.execute(select(Batch.batch_name))
    batch_names = [row[0] for row in result.all()]
    return BatchNamesResponse(batch_names=batch_names)


@app.get("/batches/check/{batch_name}", response_model=BatchNameAvailabilityResponse)
async def check_available_batch_name(
    batch_name: str,
    session: AsyncSession = Depends(get_session),
):
    """
    Check if a batch name is available (doesn't exist in database).
    """
    result = await session.execute(
        select(Batch).where(Batch.batch_name == batch_name)
    )
    batch = result.scalar_one_or_none()
    available = batch is None
    return BatchNameAvailabilityResponse(batch_name=batch_name, available=available)


@app.get("/call_logs", response_model=list[CallLogResponse])
async def get_all_call_logs(
    session: AsyncSession = Depends(get_session),
):
    """
    Get the latest call log entry.
    """
    result = await session.execute(
        select(CallLog).order_by(CallLog.started_at.desc())
    )
    call_logs = result.scalars().all()

    if not call_logs:
        raise HTTPException(status_code=404, detail="No call logs found")

    return call_logs


# --- SIP Trunk Endpoints ---
@app.post("/sip/create_outbound_trunk", response_model=SIPTrunkResponse)
async def create_sip_outbound_trunk(request: SIPTrunkRequest):
    """
    Create a SIP outbound trunk for making calls.
    """
    lkapi = get_livekit_api()
    try:
        trunk_info = SIPOutboundTrunkInfo(
            name=request.name,
            address=request.address,
            numbers=request.numbers,
            auth_username=request.auth_username,
            auth_password=request.auth_password,
            destination_country=request.destination_country,
            transport=request.transport,
        )

        create_request = CreateSIPOutboundTrunkRequest(trunk=trunk_info)
        trunk = await lkapi.sip.create_outbound_trunk(create_request)

        return SIPTrunkResponse(
            success=True,
            trunk_id=trunk.sip_trunk_id if hasattr(trunk, 'sip_trunk_id') else str(trunk),
            message=f"Successfully created trunk: {request.name}",
        )
    except Exception as e:
        logger.error(f"Failed to create SIP trunk: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await lkapi.aclose()


@app.post("/batches/{batch_name}/reset", response_model=BatchResetResponse)
async def reset_batch_leads(
    batch_name: str,
    session: AsyncSession = Depends(get_session),
):
    """
    Reset all leads in a batch to 'not called' status.
    
    - Deactivates the batch (is_active = False)
    - Resets attempt_count to 0
    - Clears status, redial_date, inflight state, last_call_at
    - Clears collected data: model, variant, time_to_buy, dealer_code
    - Sets ready_for_call to True
    """
    # Verify batch exists
    batch_result = await session.execute(
        select(Batch).where(Batch.batch_name == batch_name)
    )
    batch = batch_result.scalar_one_or_none()

    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_name}' not found")

    # Deactivate the batch
    batch.is_active = False

    # Reset all leads in the batch
    stmt = (
        update(Lead)
        .where(Lead.batch_name == batch_name)
        .values(
            attempt_count=0,
            status=None,
            redial_date=None,
            ready_for_call=True,
            inflight=False,
            inflight_room_id=None,
            last_call_at=None,
            model=None,
            variant=None,
            time_to_buy=None,
            dealer_code=None,
        )
    )
    result = await session.execute(stmt)
    await session.commit()

    return BatchResetResponse(
        batch_name=batch_name,
        reset_count=result.rowcount,
        message=f"Reset {result.rowcount} leads to 'not called' status",
    )


@app.get("/batches/{batch_name}/leads", response_model=list[LeadResponse])
async def get_batch_leads(
    batch_name: str,
    session: AsyncSession = Depends(get_session),
):
    """
    Get all leads in a batch.
    """
    # Verify batch exists
    batch_result = await session.execute(
        select(Batch).where(Batch.batch_name == batch_name)
    )
    batch = batch_result.scalar_one_or_none()

    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_name}' not found")

    # Get all leads ordered by row_order
    result = await session.execute(
        select(Lead).where(Lead.batch_name == batch_name).order_by(Lead.row_order)
    )
    leads = result.scalars().all()

    return leads
