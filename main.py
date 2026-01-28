from __future__ import annotations

# load environment variables FIRST, before any LiveKit imports
# This ensures LIVEKIT_URL is available when the CLI framework initializes
from dotenv import load_dotenv
load_dotenv(dotenv_path=".env.local")

import asyncio
import logging
import json
import os
from datetime import date, datetime, timedelta
from typing import Any, Optional
from uuid import UUID

from livekit import rtc, api
from livekit.agents import (
    AgentSession,
    Agent,
    JobContext,
    function_tool,
    RunContext,
    get_job_context,
    cli,
    WorkerOptions,
    RoomInputOptions,
)
from livekit.plugins import (
    openai,
    cartesia,
    silero,
    sarvam,
)
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from pathlib import Path

def load_products_from_json() -> list[dict[str, Any]]:
    """Load products from tata_chemicals_products.json"""
    json_path = Path(__file__).parent / "tata_chemicals_products.json"
    with open(json_path, "r") as f:
        content = f.read()
    # File contains multiple JSON objects separated by commas, wrap in array
    return json.loads(f"[{content}]")

def get_product_from_json(model_name: str) -> Optional[dict[str, Any]]:
    """Get product details by model name from JSON file"""
    products = load_products_from_json()
    model_lower = model_name.lower()
    for product in products:
        if product.get("model", "").lower() == model_lower:
            return product
    return None

from get_dealers import get_dealers


logger = logging.getLogger("outbound-caller")
logger.setLevel(logging.INFO)

# Topic for transcript data messages on LiveKit (subscribers can filter by this)
TRANSCRIPT_TOPIC = "transcript"


async def _publish_transcript_to_room(room: Any, payload: dict) -> None:
    """Publish a transcript chunk to the LiveKit room as a data message so subscribers can use it."""
    if room is None:
        return
    try:
        local = getattr(room, "local_participant", None)
        if local is None:
            return
        data = json.dumps(payload).encode("utf-8")
        await local.publish_data(data, topic=TRANSCRIPT_TOPIC)
    except Exception as e:
        logger.warning(f"Failed to publish transcript to room: {e}")


async def update_lead_in_db(
    lead_id: str,
    *,
    connected: bool,
    opty_created: bool = False,
    recall_requested: bool = False,
    not_interested: bool = False,
    agent_summary: str | None = None,
    room_id: str | None = None,
    transcript: list[dict] | None = None,
    model: str | None = None,
    variant: str | None = None,
    time_to_buy: str | None = None,
    dealer_code: str | None = None,
) -> dict:
    """
    Update lead status in the database after a call.
    
    Returns a dict with the update result.
    """
    try:
        from sqlalchemy import select, update
        from db import AsyncSessionLocal
        from models import Lead, CallLog
        
        async with AsyncSessionLocal() as session:
            # Get the lead
            result = await session.execute(
                select(Lead).where(Lead.id == UUID(lead_id))
            )
            lead = result.scalar_one_or_none()
            
            if not lead:
                logger.warning(f"Lead {lead_id} not found in database")
                return {"success": False, "error": "Lead not found"}
            
            # Increment attempt count
            lead.attempt_count += 1
            
            # Determine status based on outcome
            if connected:
                if opty_created:
                    lead.status = 1  # Success
                elif recall_requested:
                    lead.status = 0  # Recall
                    lead.redial_date = date.today() + timedelta(days=1)
                    lead.ready_for_call = False
                elif not_interested:
                    lead.status = -1  # Not interested
                # If connected but none of above, status stays as is (could be NULL or previous value)
            # If not connected, status remains unchanged (NULL stays NULL for retry)
            
            # Clear inflight status
            lead.inflight = False
            lead.inflight_room_id = None
            
            # Update call outcome details if provided
            if model:
                lead.model = model
            if variant:
                lead.variant = variant
            if time_to_buy:
                lead.time_to_buy = time_to_buy
            if dealer_code:
                lead.dealer_code = dealer_code
            
            # Update call log if room_id provided
            if room_id:
                logger.info(f"Looking for CallLog with room_id: {room_id}")
                call_log_result = await session.execute(
                    select(CallLog).where(CallLog.room_id == room_id)
                )
                call_log = call_log_result.scalar_one_or_none()
                if call_log:
                    logger.info(f"Found CallLog {call_log.id}, updating with transcript={len(transcript) if transcript else 0} items")
                    call_log.ended_at = datetime.utcnow()
                    call_log.outcome = (
                        "success" if opty_created else
                        "recall" if recall_requested else
                        "not_interested" if not_interested else
                        "connected" if connected else
                        "not_connected"
                    )
                    call_log.agent_summary = agent_summary
                    if transcript:
                        call_log.transcript = transcript
                else:
                    logger.warning(f"CallLog not found for room_id: {room_id}")
            
            await session.commit()
            
            logger.info(
                f"Updated lead {lead_id}: attempt={lead.attempt_count}, "
                f"status={lead.status}, connected={connected}"
            )
            
            return {
                "success": True,
                "lead_id": lead_id,
                "attempt_count": lead.attempt_count,
                "status": lead.status,
            }
            
    except Exception as e:
        logger.error(f"Failed to update lead {lead_id}: {e}")
        return {"success": False, "error": str(e)}


class OutboundCaller(Agent):
    def __init__(
        self,
        *,
        dial_info: dict[str, Any],
        user_details: dict[str, Any],
        lead_id: str | None = None,
        batch_name: str | None = None,
        room_id: str | None = None,
    ):
        # Format vendor details for prompt context
        vendor_context = ""
        if user_details:
            vendor_context = f"""
                ## VENDOR DETAILS (from metadata)
                - Vendor Name: {user_details.get('full_name', 'N/A')}
                - Mobile Number: {user_details.get('mobile_number', 'N/A')}
                - Previous Interest History: {user_details.get('opty_history', 'N/A')}
                - Raw Material History: {user_details.get('vahan_history', 'N/A')}
                - Call Transcripts: {user_details.get('call_transcripts', 'N/A')}
                - WhatsApp Content: {user_details.get('whatsapp_content', 'N/A')}
                - Next Best Action: {user_details.get('next_best_action', 'N/A')}
            """
        
        # Add lead_id context if from batch dialer
        lead_context = ""
        if lead_id:
            lead_context = f"""
                ## CALL TRACKING
                - Lead ID: {lead_id}
                - Batch: {batch_name or 'N/A'}
            """
        
        super().__init__(
            instructions=f"""
                
                ## IDENTITY
                You are **Nikita** from **Tata Chemicals**. Voice-only agent for vendor outreach.
                **LANGUAGE**: Speak in **Hinglish** (mix of Hindi and English). Keep it natural and conversational.
                Ensure to use the devnagri script for hindi words and roman for english words.
                Even convert the output of tool calls to devnagri script.
                All the information about raw materials you have is in english
                You must not put any symbols or numbers in the output.
                You must not put any emojis in the output.
                You must not put any special characters in the output.
                You must not put any html tags in the output.
                You must not put any markdown tags in the output.
                You must not put any xml tags in the output.
                You must not put any json tags in the output.
                You must not put any yaml tags in the output.
                You must not put any csv tags in the output.
                
                ## OBJECTIVE
                Reach out to vendors who have shown interest in providing raw materials to Tata Chemicals. Re-qualify their interest and understand their supply capabilities. Be professional, helpful and conversational.
                
                {vendor_context}
                {lead_context}
                
                ## RESTRICTIONS
                - No CRM/system references
                - Always use raw material names clearly
                - No phone/email collection
                - One question at a time, crisp responses (max 30 words)

                
                ## CALL FLOW (FLEXIBLE GUIDELINES)
                
                **Step 1: Greeting**
                "Hello, main Nikita bol rahi hoon Tata Chemicals se. Kya main {{Vendor Name}} ji se baat kar rahi hoon?"
                
                **Step 2: Reference Previous Interest**
                Use opty_history or previous interest history to find what raw material they showed interest in.
                "Aapne {{relative time}} mein {{Raw Material Name}} provide karne mein interest dikhaya tha. Kya aap abhi bhi interested hain is raw material ko supply karne mein?"
                
                **Step 3: Handle Response Naturally**
                
                **If YES**: Great! Proceed to understand their supply capacity and timeline.
                
                **If NO or NOT INTERESTED in that material**:
                - Ask: "Toh aap kaunse raw material supply kar sakte hain?" or "Toh aap konsa raw material provide karne mein interested hain?"
                - **If they mention other materials**: Acknowledge and ask about their supply capacity
                - **If they're unsure**: Proactively mention what Tata Chemicals is looking for based on Next Best Action data
                - Use get_product_details to share requirements if they want more info
                
                **Step 4: Understand Supply Capacity**
                "Aap kitna quantity supply kar sakte hain monthly? Aur aap kitne dinon mein supply start kar sakte hain?"
                
                Classify timeline response:
                - "30_days": 0-30 days, immediately, this week/month, jaldi, turant, abhi
                - "60_days": 31-60 days, 1-2 months, next month
                - "90_days": 60+ days, later, sochna hai, pata nahi
                
                **Step 5: Ask Location/Pincode**
                "Aapka manufacturing unit kahan hai? Aapka pincode kya hai? Main aapko procurement team se connect karwa sakti hoon."
                
                **Step 6: Confirm Next Steps**
                - If they're interested and ready: "Main aapke details procurement team ko forward kar deti hoon. Woh aapko jaldi contact karenge."
                - If they need time: "Theek hai, main aapko {{timeframe}} mein dobara call kar sakti hoon?"
                - Once confirmed, proceed to end call
                
                **Step 7: End Call**
                1. Say: "Dhanyavaad aapke time ke liye. Aapka din accha ho!"
                2. Call update_lead_after_call
                3. Call end_call
                
                ## RAW MATERIALS CONTEXT
                Common raw materials for Tata Chemicals: Soda Ash, Salt, Bromine, Calcium Chloride, Industrial Chemicals, etc.
                Use the information from metadata and Next Best Action to understand what specific materials are relevant.
                
                ## PRODUCT INFORMATION & SUGGESTIONS
                - Use get_product_details freely when vendor asks about any raw material requirements/specifications
                - When vendor asks "aap suggest karo" or is confused, use NBA recommendations
                - Share 2-3 key requirements based on what Tata Chemicals needs
                
                ## PINCODE HANDLING
                If vendor gives 5-digit pincode, auto-correct by inserting 0 at common positions.
                Only ask to repeat if all corrections fail.
                
                ## CLOSING AND CALL OUTCOME LOGGING
                **CRITICAL**: Before ending ANY call, call `update_lead_after_call`:
                - If vendor confirmed interest and ready to supply: opty_created=true
                - If wants callback: recall_requested=true  
                - If not interested: not_interested=true
                - If voicemail/no answer: connected=false
                - **ALWAYS** include: model (raw material name), time_to_buy_raw (supply timeline), dealer_name (vendor company name if available)
                
                ## END CALL SEQUENCE
                1. SAY: "Dhanyavaad aapke time ke liye. Aapka din accha ho!"
                2. Call update_lead_after_call
                3. Call end_call
                
                ## ANSWERING MACHINE DETECTION
                Signs: automated greetings, "leave a message", beep tones
                Action: Call update_lead_after_call with connected=false, then detected_answering_machine
                
                ## TOOL CALL BEHAVIOR
                Before ANY tool call EXCEPT end_call and update_lead_after_call, say: "Ek second, main check kar rahi hoon..."
                Do NOT say any waiting phrase before end_call or update_lead_after_call - just call them silently.
                
                ## KEY RULES
                - Be professional, conversational and helpful, not robotic
                - If vendor needs guidance, proactively mention what Tata Chemicals is looking for based on NBA
                - Use get_product_details to answer raw material requirement questions
                - Auto-correct 5-digit pincodes
                - Focus on understanding supply capacity and timeline
                - ALWAYS call update_lead_after_call before end_call
            """
        )
        # keep reference to the participant for transfers
        self.participant: rtc.RemoteParticipant | None = None

        self.dial_info = dial_info
        self.user_details = user_details
        
        # Batch dialer tracking
        self.lead_id = lead_id
        self.batch_name = batch_name
        self.room_id = room_id
        self.call_outcome_written = False
        
        # Transcript capture
        self.transcript: list[dict] = []
        # Room reference for publishing transcript data messages (set in entrypoint)
        self.room: Any = None

    def set_participant(self, participant: rtc.RemoteParticipant):
        self.participant = participant

    async def hangup(self):
        """Helper function to hang up the call by deleting the room"""

        job_ctx = get_job_context()
        await job_ctx.api.room.delete_room(
            api.DeleteRoomRequest(
                room=job_ctx.room.name,
            )
        )


    @function_tool()
    async def update_lead_after_call(
        self,
        ctx: RunContext,
        connected: bool,
        opty_created: bool = False,
        recall_requested: bool = False,
        not_interested: bool = False,
        agent_summary: str | None = None,
        model: str | None = None,
        variant: str | None = None,
        time_to_buy_raw: str | None = None,
        dealer_name: str | None = None,
    ) -> dict:
        """
        MANDATORY: Call this tool before ending ANY call to record the outcome.
        
        Args:
            connected: True if you spoke with a human, False for voicemail/no answer
            opty_created: True if customer confirmed interest and dealership
            recall_requested: True if customer wants to be called back later
            not_interested: True if customer explicitly not interested
            agent_summary: Brief summary of the conversation
            model: The car model the customer is interested in (e.g., "Nexon", "Punch", "Harrier")
            variant: The specific variant (e.g., "Fearless+ PS", "Creative+")
            time_to_buy_raw: When customer plans to buy - classify as:
                - "30_days" if within 1 month / immediately / very soon / jaldi / turant
                - "60_days" if 1-2 months / next month / couple of months
                - "90_days" if 2-3 months or more / later / sochna hai / planning
            dealer_name: The dealer/dealership name customer prefers
            
        Returns:
            Status of the database update
        """
        participant_identity = self.participant.identity if self.participant else "unknown"
        
        # Classify time_to_buy from raw input
        time_to_buy = None
        if time_to_buy_raw:
            raw_lower = time_to_buy_raw.lower()
            if any(kw in raw_lower for kw in ["immedi", "now", "today", "week", "jaldi", "turant", "abhi", "30", "1 month", "one month", "ek mahine"]):
                time_to_buy = "30_days"
            elif any(kw in raw_lower for kw in ["60", "2 month", "two month", "do mahine", "next month", "couple"]):
                time_to_buy = "60_days"
            else:
                time_to_buy = "90_days"
        
        logger.info(
            f"Recording call outcome for {participant_identity}: "
            f"connected={connected}, opty={opty_created}, recall={recall_requested}, "
            f"not_interested={not_interested}, model={model}, variant={variant}, "
            f"time_to_buy={time_to_buy}, dealer={dealer_name}, room_id={self.room_id}, "
            f"transcript_items={len(self.transcript)}"
        )
        
        # Mark that outcome was recorded
        self.call_outcome_written = True
        
        # If we have a lead_id, update the database
        if self.lead_id:
            result = await update_lead_in_db(
                lead_id=self.lead_id,
                connected=connected,
                opty_created=opty_created,
                recall_requested=recall_requested,
                not_interested=not_interested,
                agent_summary=agent_summary,
                room_id=self.room_id,
                transcript=self.transcript if self.transcript else None,
                model=model,
                variant=variant,
                time_to_buy=time_to_buy,
                dealer_code=dealer_name,
            )
            return result
        else:
            logger.info("No lead_id - skipping database update (manual/test call)")
            return {"success": True, "note": "No lead_id - manual call"}

    @function_tool()
    async def detected_answering_machine(self, ctx: RunContext):
        """CRITICAL: Call this tool IMMEDIATELY when you detect an answering machine or voicemail greeting.
        
        Detection signs:
        - Automated greeting messages
        - Phrases like "answering machine", "voicemail", "voice mail", "आंसरिंग मशीन", "वॉइस मेल"
        - Pre-recorded messages asking to leave a message
        - Beep tones
        - Any robotic/pre-recorded voice
        
        Call this tool IMMEDIATELY when ANY of these are detected, BEFORE continuing conversation.
        This will hang up the call automatically.
        
        NOTE: Call update_lead_after_call with connected=false BEFORE calling this tool."""
        participant_identity = self.participant.identity if self.participant else "unknown"
        logger.info(f"detected answering machine for {participant_identity}")
        
        # Ensure outcome is recorded before hanging up
        if not self.call_outcome_written and self.lead_id:
            await update_lead_in_db(
                lead_id=self.lead_id,
                connected=False,
                agent_summary="Answering machine/voicemail detected",
                room_id=self.room_id,
                transcript=self.transcript if self.transcript else None,
            )
            self.call_outcome_written = True
        
        await self.hangup()
    
    @function_tool()
    async def end_call(self, ctx: RunContext):
        """End the call. STRICT REQUIREMENTS:
        
        1. You MUST have already SPOKEN "Dhanyavaad aapke time ke liye. Aapka din accha ho!" OUT LOUD
        2. You MUST have already called update_lead_after_call
        
        DO NOT call this tool without completing both steps above.
        The greeting must be spoken to the user, not just thought about."""
        participant_identity = self.participant.identity if self.participant else "unknown"
        logger.info(f"ending the call for {participant_identity}")
        
        # Enforce that outcome was recorded
        if not self.call_outcome_written and self.lead_id:
            logger.warning(
                f"end_call called without update_lead_after_call for lead {self.lead_id}. "
                "Recording as connected but no outcome."
            )
            await update_lead_in_db(
                lead_id=self.lead_id,
                connected=True,  # Assume connected since we're ending normally
                agent_summary="Call ended without explicit outcome recording",
                room_id=self.room_id,
                transcript=self.transcript if self.transcript else None,
            )
            self.call_outcome_written = True
        
        await self.hangup()

    @function_tool
    async def get_product_details(self, model_name: str) -> Optional[dict[str, Any]]:
        """Get the raw material details and requirements from the product object
        Args:
            model_name: the name of the raw material to get details for
        Returns:
            the raw material details and requirements
        """
        product = get_product_from_json(model_name)
        return product
    
    @function_tool
    async def get_dealerships(self, ctx: RunContext, pincode: str) -> list[dict[str, Any]]:
        """Get the dealerships near a given pincode.
        
        Args:
            pincode: The pincode to search for nearby dealerships - must be 6 digits (e.g., "400018")
        
        Returns:
            List of dealer dictionaries with name, address, phone, etc.
            Returns error dict if pincode is invalid or API fails.
        """
        # Clean and validate pincode
        pincode = pincode.strip().replace(" ", "")
        
        # Indian pincodes are always 6 digits
        if len(pincode) != 6:
            logger.warning(f"Invalid pincode length: {pincode} ({len(pincode)} digits)")
            return [{"error": "pincode must be 6 digits", "pincode_received": pincode}]
        
        try:
            # Run sync HTTP call in thread pool to avoid blocking event loop
            dealerships = await asyncio.to_thread(get_dealers, pincode)
            
            if not dealerships:
                logger.info(f"No dealerships found for pincode: {pincode}")
                return [{"error": "no dealerships found", "pincode": pincode}]
            
            logger.info(f"Found {len(dealerships)} dealerships for pincode {pincode}")
            return dealerships
            
        except Exception as e:
            logger.error(f"Failed to fetch dealerships for {pincode}: {e}")
            return [{"error": f"failed to fetch dealerships: {str(e)}"}]




async def entrypoint(ctx: JobContext):
    logger.info(f"connecting to room {ctx.room.name}")
    await ctx.connect()
    
    # Initialize batch dialer fields
    lead_id = None
    batch_name = None
    room_id = ctx.room.name
    
    # Initialize trunk_id - will be read from metadata
    outbound_trunk_id = None
    
    if not ctx.job.metadata:
        logger.error("No metadata found in the job")
        user_details = {
            "full_name": "Tanmoy Sarkar",
            "mobile_number": "9967768395",
            "opty_history": "[{\"optyCreationDate\":\"2025-01-04\",\"carModel\":\"Punch\",\"salesStage\":\"07 Closed Lost\",\"city\":\"MUMBAI\",\"dealerName\":\"Puneet Cars, Worli - Tata Motors Service Center\",\"source\":\"Referral\",\"testDriveDate\":\"2025-01-05\"},{\"optyCreationDate\":\"2025-01-02\",\"carModel\":\"Nexon\",\"salesStage\":\"07 Closed Lost\",\"city\":\"MUMBAI\",\"dealerName\":\"Puneet Cars, Prabhadevi - Tata Motors Car Showroom\",\"source\":\"Events\"},{\"optyCreationDate\":\"2025-12-15\",\"carModel\":\"Nexon\",\"salesStage\":\"07 Closed Lost\",\"city\":\"MUMBAI\",\"dealerName\":\"Regent, Bandra - Tata Motors Car Showroom\",\"source\":\"External Leads-Web\"},{\"optyCreationDate\":\"2025-11-20\",\"carModel\":\"Tigor\",\"salesStage\":\"07 Closed Lost\",\"city\":\"MUMBAI\",\"dealerName\":\"Puneet Cars, Worli - Tata Motors Service Center\",\"source\":\"External Leads-Web\",\"testDriveDate\":\"2025-11-25\"},{\"optyCreationDate\":\"2025-10-10\",\"carModel\":\"Altroz\",\"salesStage\":\"07 Closed Lost\",\"city\":\"MUMBAI\",\"dealerName\":\"Regent, Bandra - Tata Motors Car Showroom\",\"source\":\"External Leads-Web\",\"testDriveDate\":\"2025-10-15\"}]",
            "vahan_history": "[{\"carModel\":\"ALTO K10 VXI CNG\",\"manufacturer\":\"MARUTI SUZUKI INDIA LTD\",\"registrationDate\":\"2023-09-10\",\"rtoLocation\":\"MUMBAI\",\"rtoState\":\"Maharashtra\",\"seatCapacity\":4,\"cubicCapacity\":998}]",
            "call_transcripts": "[\"Customer called to inquire about Punch Adventure model. Interested in safety features and ADAS. Budget around 10-12 lakhs.\",\"Follow-up call: Customer mentioned comparing with Hyundai Venue. Emphasized Nexon's 5-star safety rating.\"]",
            "whatsapp_content": "[\"Thank you for contacting Chalo Apni Rides! Please let us know how we can help you.\",\"Hi, I am interested in the Nexon model. Can you share the price?\",\"What are the finance options available?\"]",
            "next_best_action": "Customer Interest: Safety conscious; Modern features biased | Relevant Features: Level 2+ ADAS suite; Voice-assisted panoramic sunroof; Advanced infotainment | Recommended Models: Nexon Fearless+ PS (DCA), Harrier Accomplished Ultra | Suggestions: Highlight Nexon's 5-star safety ratings and ADAS; Emphasize Harrier's Samsung Neo QLED connectivity"
        }

        dial_info = {"phone_number": "+919806953395", "full_name": "John Doe"}
        # Fallback to env var for test/manual calls without metadata
        outbound_trunk_id = os.getenv("SIP_OUTBOUND_TRUNK_ID")
    else:
        logger.info("Metadata found in the job")
        logger.info(ctx.job.metadata)
        metadata = json.loads(ctx.job.metadata)
        
        # Get trunk_id from metadata (set by celery task)
        outbound_trunk_id = metadata.get("sip_trunk_id")
        
        # Extract batch dialer fields if present
        lead_id = metadata.get("lead_id")
        batch_name = metadata.get("batch_name")
        
        if lead_id:
            logger.info(f"Batch dialer call: lead_id={lead_id}, batch={batch_name}")
        
        # Extract user details from metadata
        # Support both direct fields and nested lead_metadata from batch dialer
        lead_metadata = metadata.get("lead_metadata", {}) or {}
        
        user_details = {
            "full_name": metadata.get("full_name") or metadata.get("name", ""),
            "mobile_number": metadata.get("mobile_number") or metadata.get("phone", ""),
            "opty_history": lead_metadata.get("opty_history", metadata.get("opty_history", "")),
            "vahan_history": lead_metadata.get("vahan_history", metadata.get("vahan_history", "")),
            "call_transcripts": lead_metadata.get("call_transcripts", metadata.get("call_transcripts", "")),
            "whatsapp_content": lead_metadata.get("whatsapp_content", metadata.get("whatsapp_content", "")),
            "next_best_action": lead_metadata.get("next_best_action", metadata.get("next_best_action", ""))
        }
        
        # Extract dial info (phone_number and full_name for dialing)
        dial_info = {
            "phone_number": metadata.get("phone") or metadata.get("mobile_number", ""),
            "full_name": metadata.get("name") or metadata.get("full_name", "")
        }

    # when dispatching the agent, we'll pass it the approriate info to dial the user
    # dial_info is a dict with the following keys:
    # - phone_number: the phone number to dial
    # - transfer_to: the phone number to transfer the call to when requested
    phone_number = (dial_info.get("phone_number") or "").strip()
    participant_identity = (dial_info.get("full_name") or "unknown").strip() or "unknown"
    full_name = participant_identity
    logger.info(f"full_name: {full_name}")
    logger.info(f"user_details: {user_details}")

    if not phone_number:
        logger.error(
            "Missing SIP callee number: metadata must include 'phone' or 'mobile_number'. "
            "When dispatching the agent, pass metadata with phone/mobile_number set."
        )
        if lead_id:
            asyncio.create_task(update_lead_in_db(
                lead_id=lead_id,
                connected=False,
                agent_summary="Agent error: missing phone number in metadata",
                room_id=room_id,
            ))
        ctx.shutdown()
        return

    # look up the user's phone number and appointment details
    agent = OutboundCaller(
        dial_info=dial_info,
        user_details=user_details,
        lead_id=lead_id,
        batch_name=batch_name,
        room_id=room_id,
    )
    agent.room = ctx.room  # for publishing transcript to LiveKit

    # the following uses GPT-4o, Deepgram and Cartesia
    session = AgentSession(
        # turn_detection=MultilingualModel(),  # Temporarily disabled - requires model download
        # Use VAD-based turn detection instead (simpler, no model files needed)
        vad=silero.VAD.load(
            activation_threshold=0.9,      # Lower = more sensitive (default: 0.5)
            min_speech_duration=1,      # Min speech duration to trigger (default: 0.05s)
            min_silence_duration=1,     # Silence before ending speech (default: 0.55s)
        ),
        stt=sarvam.STT(model = 'saarika:v2.5', language='hi-IN'),
        # stt=deepgram.STT(language='en'),
        # you can also use OpenAI's TTS with openai.TTS()
        tts=cartesia.TTS(model ='sonic-2',voice='56e35e2d-6eb6-4226-ab8b-9776515a7094',language='hi'),
        # tts=cartesia.TTS(language='en'),
        # llm=aws.LLM(model="anthropic.claude-3-haiku-20240307-v1:0"),
        llm=openai.LLM(model="gpt-5.2"),
        # you can also use a speech-to-speech model like OpenAI's Realtime API
        # llm=openai.realtime.RealtimeModel()
        allow_interruptions=False,
    )

    # Register cleanup handler for when session ends (handles unexpected disconnects)
    @session.on("close")
    def on_session_close():
        logger.info(f"Session closed for room {room_id}")
        if lead_id and not agent.call_outcome_written:
            logger.warning(f"Session ended without outcome recorded for lead {lead_id}. Recording as not_connected.")
            agent.call_outcome_written = True
            asyncio.create_task(update_lead_in_db(
                lead_id=lead_id,
                connected=False,
                agent_summary="Session ended unexpectedly - no outcome recorded",
                room_id=room_id,
                transcript=agent.transcript if agent.transcript else None,
            ))

    # Capture user transcripts as they arrive (final only)
    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        """Capture finalized user speech and publish to LiveKit room."""
        try:
            if event.is_final and event.transcript:
                chunk = {
                    "event": "transcript",
                    "role": "user",
                    "text": event.transcript,
                    "timestamp": datetime.utcnow().isoformat(),
                    "room_id": room_id,
                }
                agent.transcript.append({
                    "role": "user",
                    "text": event.transcript,
                    "timestamp": chunk["timestamp"],
                })
                logger.info(f"Transcript [user]: {event.transcript[:100]}...")
                if agent.room:
                    asyncio.create_task(_publish_transcript_to_room(agent.room, chunk))
        except Exception as e:
            logger.warning(f"Failed to capture user transcript: {e}")

    # Capture agent responses as conversation items are added
    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        """Capture agent responses for transcript."""
        try:
            item = event.item
            role = getattr(item, "role", "unknown")
            
            # Skip user items - we capture those via user_input_transcribed
            if role == "user":
                return
            
            # Use text_content property if available, otherwise build from content list
            content = ""
            if hasattr(item, "text_content") and item.text_content:
                content = item.text_content
            elif hasattr(item, "content"):
                # Build content from content list
                parts = []
                for part in item.content:
                    if isinstance(part, str):
                        parts.append(part)
                    elif hasattr(part, "transcript") and part.transcript:
                        # AudioContent with transcript
                        parts.append(part.transcript)
                    elif hasattr(part, "text"):
                        parts.append(part.text)
                content = " ".join(parts)
            
            if content:  # Only add non-empty entries
                ts = datetime.utcnow().isoformat()
                agent.transcript.append({
                    "role": role,
                    "text": content,
                    "timestamp": ts,
                })
                logger.info(f"Transcript [{role}]: {content[:100]}...")
                chunk = {
                    "event": "transcript",
                    "role": role,
                    "text": content,
                    "timestamp": ts,
                    "room_id": room_id,
                }
                if agent.room:
                    asyncio.create_task(_publish_transcript_to_room(agent.room, chunk))
        except Exception as e:
            logger.warning(f"Failed to capture transcript item: {e}")

    # start the session first before dialing, to ensure that when the user picks up
    # the agent does not miss anything the user says
    session_started = asyncio.create_task(
        session.start(
            agent=agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(
                # enable Krisp background voice and noise removal
                # noise_cancellation=noise_cancellation.BVCTelephony(),
            ),
        )
    )

    # `create_sip_participant` starts dialing the user
    if not outbound_trunk_id:
        logger.error("Cannot create SIP participant: SIP_OUTBOUND_TRUNK_ID is not set")
        ctx.shutdown()
        return
    
    logger.info(f"Initiating SIP call to {phone_number} using trunk {outbound_trunk_id}")
    try:
        await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=outbound_trunk_id,
                sip_call_to=phone_number,
                participant_identity=participant_identity,
                # function blocks until user answers the call, or if the call fails
                wait_until_answered=True,
            )
        )

        # wait for the agent session start and participant join
        await session_started
        participant = await ctx.wait_for_participant(identity=participant_identity)
        await session.generate_reply(
            instructions=f"Greet the vendor in hindi, eg. of English: Hello! I am Nikita from Tata Chemicals. Am I speaking with {full_name}?",
            allow_interruptions=False
        )
        logger.info(f"participant joined: {participant.identity}")

        agent.set_participant(participant)
        
        # Start 3-minute call timeout
        async def call_timeout():
            await asyncio.sleep(150)  # 2.5 minutes
            if not agent.call_outcome_written:
                logger.info(f"Call timeout reached (3 min) for lead {lead_id}, ending call")
                if lead_id:
                    await update_lead_in_db(
                        lead_id=lead_id,
                        connected=True,
                        recall_requested=True,  # Schedule callback for tomorrow, prevent immediate redial
                        agent_summary="Call ended due to 3-minute timeout",
                        room_id=room_id,
                        transcript=agent.transcript if agent.transcript else None,
                    )
                agent.call_outcome_written = True
                await agent.hangup()
        
        asyncio.create_task(call_timeout())

    except api.TwirpError as e:
        sip_code = e.metadata.get("sip_status_code") or ""
        sip_status = e.metadata.get("sip_status", "unknown")
        logger.error(
            f"error creating SIP participant: {e.message}, "
            f"SIP status: {sip_code} {sip_status}"
        )
        # 486 User Busy (or similar) can arrive after the callee already answered and joined.
        # If we already have a participant in the room, continue and speak so the agent is heard.
        if sip_code == "486" or "busy" in (sip_status or "").lower():
            try:
                await session_started
                participant = await asyncio.wait_for(
                    ctx.wait_for_participant(identity=participant_identity),
                    timeout=5.0,
                )
                logger.info(f"Participant joined despite 486; sending greeting. participant={participant.identity}")
                await session.generate_reply(
                    instructions=f"Greet the vendor in hindi, eg. of English: Hello! I am Nikita from Tata Chemicals. Am I speaking with {full_name}?",
                    allow_interruptions=False,
                )
                agent.set_participant(participant)
                # Start call timeout as in the success path
                async def call_timeout():
                    await asyncio.sleep(150)
                    if not agent.call_outcome_written:
                        if lead_id:
                            await update_lead_in_db(
                                lead_id=lead_id,
                                connected=True,
                                recall_requested=True,
                                agent_summary="Call ended due to 3-minute timeout",
                                room_id=room_id,
                                transcript=agent.transcript if agent.transcript else None,
                            )
                        agent.call_outcome_written = True
                        await agent.hangup()
                asyncio.create_task(call_timeout())
                return
            except (asyncio.TimeoutError, Exception) as fallback_err:
                logger.warning(f"Could not recover after 486: {fallback_err}")
        # No participant or not 486: treat as failed SIP and shutdown
        if lead_id and not agent.call_outcome_written:
            await update_lead_in_db(
                lead_id=lead_id,
                connected=False,
                agent_summary=f"SIP call failed: {sip_status}",
                room_id=room_id,
            )
            agent.call_outcome_written = True
        ctx.shutdown()


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="tcpl-tatachem-v2v-agent",
        )
    )
