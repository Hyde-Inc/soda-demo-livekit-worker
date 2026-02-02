"""LiveKit agent dispatch module."""

import json
import logging
import os
from datetime import datetime
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env.local")

from livekit import api
from livekit.protocol.sip import (
    CreateSIPOutboundTrunkRequest,
    SIPOutboundTrunkInfo,
    ListSIPOutboundTrunkRequest,
)

logger = logging.getLogger(__name__)

# LiveKit configuration
LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")

# Egress recording configuration
EGRESS_S3_BUCKET = os.getenv("EGRESS_S3_BUCKET")

# SIP Trunk configuration (for creating new trunks)
SIP_TRUNK_NAME = os.getenv("SIP_TRUNK_NAME", "Default Outbound Trunk")
SIP_TRUNK_ADDRESS = os.getenv("SIP_TRUNK_ADDRESS")
SIP_TRUNK_NUMBERS = os.getenv("SIP_TRUNK_NUMBERS", "").split(",")  # comma-separated
SIP_TRUNK_AUTH_USERNAME = os.getenv("SIP_TRUNK_AUTH_USERNAME")
SIP_TRUNK_AUTH_PASSWORD = os.getenv("SIP_TRUNK_AUTH_PASSWORD")
SIP_TRUNK_DESTINATION_COUNTRY = os.getenv("SIP_TRUNK_DESTINATION_COUNTRY", "INDIA")

# Cached trunk ID
_cached_trunk_id: Optional[str] = None


def _get_livekit_api_url() -> str:
    """Convert ws:// to http:// for API calls."""
    url = LIVEKIT_URL
    if url and url.startswith("ws://"):
        return url.replace("ws://", "http://")
    elif url and url.startswith("wss://"):
        return url.replace("wss://", "https://")
    return url


async def get_or_create_sip_trunk() -> Optional[str]:
    """
    Get existing SIP outbound trunk ID or create a new one.
    Prefers SIP_OUTBOUND_TRUNK_ID from env if set; otherwise picks a trunk by SIP_TRUNK_NAME
    (so the caller number matches your config), then falls back to the first trunk.
    
    Returns:
        trunk_id if found/created, None on failure
    """
    global _cached_trunk_id
    
    # Prefer explicit trunk ID from env (so you can pin to a specific caller number)
    env_trunk_id = os.getenv("SIP_OUTBOUND_TRUNK_ID", "").strip()
    if env_trunk_id:
        logger.info(f"Using SIP trunk from env: {env_trunk_id}")
        _cached_trunk_id = env_trunk_id
        return env_trunk_id
    
    # Return cached trunk ID if available
    if _cached_trunk_id:
        logger.debug(f"Using cached trunk ID: {_cached_trunk_id}")
        return _cached_trunk_id
    
    api_url = _get_livekit_api_url()
    lk_api = api.LiveKitAPI(
        url=api_url,
        api_key=LIVEKIT_API_KEY,
        api_secret=LIVEKIT_API_SECRET,
    )
    
    try:
        # List existing outbound trunks
        request = ListSIPOutboundTrunkRequest()
        response = await lk_api.sip.list_outbound_trunk(request)
        
        # Check if any trunks exist
        if response.items:
            # Prefer trunk whose name matches SIP_TRUNK_NAME (so caller number matches your .env)
            if SIP_TRUNK_NAME:
                for item in response.items:
                    item_name = getattr(item, "name", None)
                    if item_name is None and hasattr(item, "trunk"):
                        item_name = getattr(item.trunk, "name", None)
                    if isinstance(item_name, str) and item_name.strip() == SIP_TRUNK_NAME.strip():
                        trunk_id = item.sip_trunk_id
                        logger.info(f"Using SIP trunk matching name '{SIP_TRUNK_NAME}': {trunk_id}")
                        _cached_trunk_id = trunk_id
                        return trunk_id
            # No matching trunk but SIP_TRUNK_* is set: create one with your number so calls use it
            if all([SIP_TRUNK_ADDRESS, SIP_TRUNK_AUTH_USERNAME, SIP_TRUNK_AUTH_PASSWORD]) and SIP_TRUNK_NUMBERS:
                logger.info(f"No trunk named '{SIP_TRUNK_NAME}', creating one with configured numbers...")
                trunk_info = SIPOutboundTrunkInfo(
                    name=SIP_TRUNK_NAME,
                    address=SIP_TRUNK_ADDRESS,
                    numbers=[n.strip() for n in SIP_TRUNK_NUMBERS if n.strip()],
                    auth_username=SIP_TRUNK_AUTH_USERNAME,
                    auth_password=SIP_TRUNK_AUTH_PASSWORD,
                    destination_country=SIP_TRUNK_DESTINATION_COUNTRY,
                    transport=1,
                )
                create_request = CreateSIPOutboundTrunkRequest(trunk=trunk_info)
                created_trunk = await lk_api.sip.create_outbound_trunk(create_request)
                trunk_id = created_trunk.sip_trunk_id
                logger.info(f"Created and using SIP trunk '{SIP_TRUNK_NAME}': {trunk_id}")
                _cached_trunk_id = trunk_id
                return trunk_id
            # Fallback: use first trunk (previous behavior)
            trunk_id = response.items[0].sip_trunk_id
            logger.info(f"Using first existing SIP trunk: {trunk_id}")
            _cached_trunk_id = trunk_id
            return trunk_id
        
        # No trunk exists, create one
        logger.info("No SIP trunk found, creating new one...")
        
        if not all([SIP_TRUNK_ADDRESS, SIP_TRUNK_AUTH_USERNAME, SIP_TRUNK_AUTH_PASSWORD]):
            logger.error(
                "SIP trunk config missing. Set SIP_TRUNK_ADDRESS, "
                "SIP_TRUNK_AUTH_USERNAME, SIP_TRUNK_AUTH_PASSWORD"
            )
            return None
        
        trunk_info = SIPOutboundTrunkInfo(
            name=SIP_TRUNK_NAME,
            address=SIP_TRUNK_ADDRESS,
            numbers=[n.strip() for n in SIP_TRUNK_NUMBERS if n.strip()],
            auth_username=SIP_TRUNK_AUTH_USERNAME,
            auth_password=SIP_TRUNK_AUTH_PASSWORD,
            destination_country=SIP_TRUNK_DESTINATION_COUNTRY,
            transport=1,  # SIP_TRANSPORT_TCP
        )
        
        create_request = CreateSIPOutboundTrunkRequest(trunk=trunk_info)
        created_trunk = await lk_api.sip.create_outbound_trunk(create_request)
        
        trunk_id = created_trunk.sip_trunk_id
        logger.info(f"Created new SIP trunk: {trunk_id}")
        _cached_trunk_id = trunk_id
        return trunk_id
        
    except Exception as e:
        logger.error(f"Failed to get/create SIP trunk: {e}")
        return None
    
    finally:
        await lk_api.aclose()


def _build_room_egress_config(room_name: str, phone_number: str | None = None) -> Optional[api.RoomEgress]:
    """
    Build egress configuration for automatic call recording.
    
    Records audio-only in dual channel mode:
    - Left channel: Agent (first participant)
    - Right channel: Customer (other participants)
    
    Returns None if EGRESS_S3_BUCKET is not configured.
    """
    if not EGRESS_S3_BUCKET:
        logger.warning("EGRESS_S3_BUCKET not set, call recording disabled")
        return None
    
    # Clean phone number for filename (remove +, spaces, etc.)
    clean_phone = ""
    if phone_number:
        clean_phone = "".join(c for c in phone_number if c.isdigit())
    
    # Build filepath: mobile_number-room_name.mp3
    # Use {time} template for uniqueness if phone not available
    if clean_phone:
        filepath = f"recordings/{clean_phone}-{{room_name}}-{{time}}.mp3"
    else:
        filepath = f"recordings/{{room_name}}_{{time}}.mp3"
    
    # Create S3 upload config (credentials come from egress server env)
    s3_upload = api.S3Upload(bucket=EGRESS_S3_BUCKET)
    
    # Create file output config with explicit MP3 file type
    file_output = api.EncodedFileOutput(
        filepath=filepath,
        file_type=api.EncodedFileType.MP3,  # Explicitly set MP3 format
        s3=s3_upload,
    )
    
    # For auto-egress, don't set room_name - it's derived from the room being created
    room_composite = api.RoomCompositeEgressRequest(
        audio_only=True,
        audio_mixing=api.AudioMixing.DUAL_CHANNEL_AGENT,  # Agent on left, others on right
        file_outputs=[file_output],
    )
    
    # Wrap in RoomEgress for auto-egress
    egress_config = api.RoomEgress(room=room_composite)
    
    logger.info(f"Configured auto-egress: {filepath} -> s3://{EGRESS_S3_BUCKET}")
    return egress_config


async def dispatch_agent_for_lead(
    *,
    room_name: str,
    metadata: dict[str, Any],
) -> str:
    """
    Create a LiveKit room and dispatch the outbound caller agent.

    Args:
        room_name: Unique room name for this call
        metadata: Dict containing lead_id, batch_name, phone, attempt_count, etc.

    Returns:
        The room name/ID
    """
    if not all([LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET]):
        raise ValueError(
            "LiveKit configuration missing. "
            "Set LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET"
        )

    # Create LiveKit API client
    lk_api = api.LiveKitAPI(
        url=LIVEKIT_URL,
        api_key=LIVEKIT_API_KEY,
        api_secret=LIVEKIT_API_SECRET,
    )

    try:
        # Extract phone number from metadata for recording filename
        phone_number = metadata.get("phone") or metadata.get("mobile_number")
        
        # Build egress config for call recording
        egress_config = _build_room_egress_config(room_name, phone_number)
        
        if egress_config:
            logger.info(f"Egress config: audio_only={egress_config.room.audio_only}, bucket={EGRESS_S3_BUCKET}")
        else:
            logger.warning("No egress config - recordings disabled. Set EGRESS_S3_BUCKET env var.")
        
        # Create the room - pass egress in constructor (protobuf doesn't allow post-assignment)
        room = await lk_api.room.create_room(
            api.CreateRoomRequest(
                name=room_name,
                empty_timeout=300,  # 5 minutes
                max_participants=3,  # Agent + SIP participant + buffer
                metadata=json.dumps(metadata),
                egress=egress_config,  # None is fine if not configured
            )
        )
        logger.info(f"Created room: {room.name}" + (" with auto-egress recording" if egress_config else " (no recording)"))

        # Dispatch the agent to the room
        # The agent is identified by its name from WorkerOptions in main.py
        agent_dispatch = await lk_api.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                room=room_name,
                agent_name="tcpl-tatachem-v2v-agent",
                metadata=json.dumps(metadata),
            )
        )
        logger.info(f"Dispatched agent to room: {room_name}, dispatch_id={agent_dispatch.id}")

        return room_name

    except Exception as e:
        logger.error(f"Failed to dispatch agent: {e}")
        # Try to clean up the room if it was created
        try:
            await lk_api.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:
            pass
        raise

    finally:
        await lk_api.aclose()
