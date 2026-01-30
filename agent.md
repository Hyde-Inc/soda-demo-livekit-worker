# Tata Chemicals Voice-to-Voice Agent

## Overview

This agent is a voice-to-voice conversational AI agent for Tata Chemicals that makes outbound calls to vendors to re-qualify their interest in providing raw materials. The agent speaks in Hinglish (Hindi + English) and conducts natural conversations to understand vendor supply capabilities, timelines, and locations.

## Features

- **Voice-to-Voice Communication**: Real-time bidirectional voice conversation using LiveKit
- **Multilingual Support**: Speaks in Hinglish (Hindi + English) with Devanagari script for Hindi words
- **Speech-to-Text**: Uses Sarvam AI for Hindi speech recognition (model: `saarika:v2.5`)
- **Text-to-Speech**: Uses Cartesia for natural Hindi voice synthesis (model: `sonic-2`)
- **LLM**: Powered by OpenAI GPT-5.2 for natural conversation
- **SIP Telephony**: Makes outbound phone calls via SIP trunk integration
- **Call Outcome Tracking**: Automatically logs call results, transcripts, and vendor responses
- **Answering Machine Detection**: Automatically detects and handles voicemail/answering machines
- **Product Information**: Accesses raw material details and requirements from product database

## Agent Workflow

1. **Call Initiation**: Agent receives job dispatch with vendor metadata (name, phone, previous interest history)
2. **SIP Connection**: Creates SIP participant to dial vendor's phone number
3. **Greeting**: Agent greets vendor in Hinglish: "Hello, main Nikita bol rahi hoon Tata Chemicals se..."
4. **Interest Re-qualification**: References previous interest in raw materials and confirms current interest
5. **Supply Capacity**: Understands vendor's monthly supply capacity and timeline
6. **Location Collection**: Gets manufacturing unit location and pincode
7. **Next Steps**: Confirms follow-up actions and connects vendor with procurement team
8. **Call Logging**: Records call outcome, transcript, and vendor details in database
9. **Call Termination**: Ends call with proper closing greeting

## Tools

### `get_product_details`
Retrieves raw material details and requirements from the product database.

**Parameters:**
- `model_name` (str): Name of the raw material (e.g., "Soda Ash", "Salt", "Bromine")

**Returns:**
- Product details including specifications, requirements, and availability information

**Usage:**
Agent uses this when vendor asks about specific raw material requirements or when proactively sharing information about what Tata Chemicals needs.

### `get_dealerships`
Fetches nearby locations/dealerships based on pincode. (Note: Currently configured for dealership lookup, can be adapted for procurement offices)

**Parameters:**
- `pincode` (str): 6-digit Indian pincode (e.g., "400018")

**Returns:**
- List of locations with name, address, phone, etc.
- Error dict if pincode is invalid or API fails

**Usage:**
Agent uses this to find nearby procurement offices or relevant locations based on vendor's manufacturing unit location.

### `update_lead_after_call`
**MANDATORY**: Records call outcome before ending any call. Must be called before `end_call`.

**Parameters:**
- `connected` (bool): True if spoke with human, False for voicemail/no answer
- `opty_created` (bool): True if vendor confirmed interest and ready to supply
- `recall_requested` (bool): True if vendor wants callback later
- `not_interested` (bool): True if vendor explicitly not interested
- `agent_summary` (str, optional): Brief summary of conversation
- `model` (str, optional): Raw material name vendor is interested in
- `variant` (str, optional): Specific variant or type
- `time_to_buy_raw` (str, optional): Supply timeline (classified as "30_days", "60_days", or "90_days")
- `dealer_name` (str, optional): Vendor company name

**Returns:**
- Status of database update with success/error information

**Timeline Classification:**
- `30_days`: 0-30 days, immediately, this week/month, jaldi, turant, abhi
- `60_days`: 31-60 days, 1-2 months, next month
- `90_days`: 60+ days, later, sochna hai, pata nahi

### `detected_answering_machine`
**CRITICAL**: Detects and handles answering machines/voicemail. Must be called immediately when detected.

**Detection Signs:**
- Automated greeting messages
- Phrases like "answering machine", "voicemail", "आंसरिंग मशीन", "वॉइस मेल"
- Pre-recorded messages asking to leave a message
- Beep tones
- Any robotic/pre-recorded voice

**Behavior:**
- Automatically hangs up the call
- Records outcome as `connected=false` in database
- Must be called after `update_lead_after_call` with `connected=false`

### `end_call`
Terminates the call. Has strict requirements:

**Requirements:**
1. Agent MUST have already SPOKEN the closing greeting: "Dhanyavaad aapke time ke liye. Aapka din accha ho!" OUT LOUD
2. Agent MUST have already called `update_lead_after_call`

**Behavior:**
- Hangs up the call
- Verifies that outcome was recorded
- If outcome not recorded, automatically records as connected with warning

## Usage Example

### Manual Call Dispatch

```python
# Via LiveKit API
from livekit import api

lk_api = api.LiveKitAPI(
    url="wss://your-livekit-server.livekit.cloud",
    api_key="your_api_key",
    api_secret="your_api_secret"
)

# Create room with metadata
room = await lk_api.room.create_room(
    api.CreateRoomRequest(
        name="call-vendor-123",
        metadata=json.dumps({
            "phone": "+919876543210",
            "name": "ABC Chemicals Pvt Ltd",
            "full_name": "Rajesh Kumar",
            "lead_metadata": {
                "opty_history": "[{\"rawMaterial\":\"Soda Ash\",\"date\":\"2025-01-15\"}]",
                "next_best_action": "Interested in Soda Ash supply"
            },
            "sip_trunk_id": "trunk_abc123"
        })
    )
)

# Agent automatically connects and makes call
```

### Batch Dialer Integration

The agent integrates with a batch dialer system that:
1. Reads vendor leads from database
2. Creates LiveKit rooms with vendor metadata
3. Dispatches agent jobs automatically
4. Tracks call outcomes and updates lead status

## Environment Variables

### Required

- `LIVEKIT_URL`: WebSocket URL for LiveKit server (e.g., `wss://your-server.livekit.cloud`)
- `LIVEKIT_API_KEY`: LiveKit API key
- `LIVEKIT_API_SECRET`: LiveKit API secret
- `SIP_OUTBOUND_TRUNK_ID`: SIP trunk ID for making outbound calls
- `OPENAI_API_KEY`: OpenAI API key for GPT-5.2 model
- `SARVAM_API_KEY`: Sarvam AI API key for Hindi speech-to-text
- `CARTESIA_API_KEY`: Cartesia API key for text-to-speech

### Optional

- `DATABASE_URL`: PostgreSQL connection string (for lead tracking)
- `REDIS_URL`: Redis connection string (for Celery task queue)
- `EGRESS_S3_BUCKET`: S3 bucket for call recordings
- `AWS_ACCESS_KEY_ID`: AWS access key (for S3 recordings)
- `AWS_SECRET_ACCESS_KEY`: AWS secret key
- `AWS_DEFAULT_REGION`: AWS region

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt
# OR using uv (recommended)
uv sync

# Set up environment
cp env.example .env.local
# Edit .env.local with your secrets

# Download required model files
python main.py download-files

# Start the agent
python main.py start
```

## Call Flow

### Step 1: Greeting
```
Agent: "Hello, main Nikita bol rahi hoon Tata Chemicals se. Kya main [Vendor Name] ji se baat kar rahi hoon?"
```

### Step 2: Reference Previous Interest
```
Agent: "Aapne [relative time] mein [Raw Material Name] provide karne mein interest dikhaya tha. 
        Kya aap abhi bhi interested hain is raw material ko supply karne mein?"
```

### Step 3: Understand Supply Capacity
```
Agent: "Aap kitna quantity supply kar sakte hain monthly? 
        Aur aap kitne dinon mein supply start kar sakte hain?"
```

### Step 4: Location Collection
```
Agent: "Aapka manufacturing unit kahan hai? Aapka pincode kya hai? 
        Main aapko procurement team se connect karwa sakti hoon."
```

### Step 5: Closing
```
Agent: "Dhanyavaad aapke time ke liye. Aapka din accha ho!"
# Then calls update_lead_after_call and end_call
```

## Input Format

The agent receives metadata via LiveKit job context:

```json
{
  "phone": "+919876543210",
  "name": "ABC Chemicals Pvt Ltd",
  "full_name": "Rajesh Kumar",
  "lead_id": "uuid-here",
  "batch_name": "soda-ash-vendors-2025",
  "lead_metadata": {
    "opty_history": "[{\"rawMaterial\":\"Soda Ash\",\"date\":\"2025-01-15\"}]",
    "vahan_history": "[]",
    "call_transcripts": "[]",
    "whatsapp_content": "[]",
    "next_best_action": "Interested in Soda Ash supply, capacity 1000 MT/month"
  },
  "sip_trunk_id": "trunk_abc123"
}
```

## Output Format

Call outcomes are logged to database with:

```json
{
  "success": true,
  "lead_id": "uuid-here",
  "attempt_count": 1,
  "status": 1,
  "connected": true,
  "opty_created": true,
  "model": "Soda Ash",
  "time_to_buy": "30_days",
  "dealer_code": "ABC Chemicals",
  "transcript": [
    {
      "role": "user",
      "text": "Haan, main interested hoon",
      "timestamp": "2025-01-27T14:00:00Z"
    },
    {
      "role": "assistant",
      "text": "Great! Aap kitna quantity supply kar sakte hain?",
      "timestamp": "2025-01-27T14:00:05Z"
    }
  ],
  "agent_summary": "Vendor confirmed interest in Soda Ash. Can supply 1000 MT/month. Ready to start in 30 days."
}
```

## Integration

### With Batch Dialer System

1. **Lead Upload**: Upload vendor CSV with columns: name, phone, previous_interest, etc.
2. **Batch Creation**: Create batch via FastAPI endpoint `/api/batches/upload`
3. **Automatic Dialing**: Celery worker dispatches calls based on batch configuration
4. **Outcome Tracking**: All call outcomes automatically logged to database
5. **Reporting**: Query call logs and lead status via API endpoints

### With LiveKit Server

The agent connects to LiveKit server via WebSocket:
- Worker registers with server on startup
- Server dispatches jobs when rooms are created
- Agent connects to room and initiates SIP call
- Real-time audio streaming via WebRTC

### SIP Trunk Configuration

Configure SIP trunk for outbound calling:
- Set `SIP_OUTBOUND_TRUNK_ID` environment variable
- Or create trunk programmatically via `dispatch_agent.py`
- Trunk must support outbound calling to target phone numbers

## Technical Stack

- **Framework**: LiveKit Agents SDK
- **LLM**: OpenAI GPT-5.2
- **STT**: Sarvam AI (Hindi - `saarika:v2.5`)
- **TTS**: Cartesia (Hindi - `sonic-2`, voice ID: `56e35e2d-6eb6-4226-ab8b-9776515a7094`)
- **VAD**: Silero Voice Activity Detection
- **Turn Detection**: MultilingualModel from LiveKit
- **Database**: PostgreSQL (via SQLAlchemy)
- **Task Queue**: Celery with Redis
- **API Server**: FastAPI

## Notes

- Agent speaks in **Hinglish** - mix of Hindi (Devanagari script) and English (Roman script)
- All Hindi words must be in Devanagari script in output
- Agent has 3-minute call timeout (automatically ends long calls)
- Answering machine detection is automatic and hangs up immediately
- Call transcripts are captured in real-time and stored in database
- Agent can handle interruptions but currently configured with `allow_interruptions=False`
- Product information loaded from `tata_chemicals_products.json` file
- Supports both manual calls (via API) and batch dialing (via Celery)

## Transcript Data Messages (LiveKit)

The agent publishes **transcript chunks** to the LiveKit room as **data messages** so other services (dashboards, analytics, recording pipelines) can consume them.

- **Topic**: `transcript`
- **Payload** (JSON): `{"event": "transcript", "role": "user"|"assistant", "text": "...", "timestamp": "...", "room_id": "..."}`
- **When**: Each time the user speaks (final transcript) or the agent speaks (conversation item added).

To consume on the server or in a client:

1. Subscribe to the room’s data messages (filter by topic `transcript`).
2. Parse the JSON payload; `role` is `user` or the agent role (e.g. `assistant`), `text` is the utterance, `room_id` identifies the call.

## Error Handling

- **SIP Call Failure**: Logs error, updates lead as `connected=false`
- **No Answer**: Detected via SIP timeout, logged as not connected
- **Answering Machine**: Automatically detected and call terminated
- **Session Disconnect**: Cleanup handler records outcome as not connected
- **Database Errors**: Logged but don't crash the agent
- **Missing Metadata**: Falls back to test/default values for development
