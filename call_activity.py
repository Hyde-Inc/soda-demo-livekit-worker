"""Call activity recorder — captures voice agent events and persists to Supabase.

Wraps both LiveKit room publishing (real-time UI) and Supabase writes (persistence)
behind a single interface. If the backend changes from Supabase, only this file needs
to change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger("call-activity")

TRANSCRIPT_TOPIC = "transcript"


class CallActivityRecorder:

    def __init__(self, supabase_url: str = "", supabase_key: str = ""):
        self._url = supabase_url.rstrip("/") if supabase_url else ""
        self._key = supabase_key
        self._run_id: str | None = None
        self._workflow_id: str = ""
        self._room_id: str = ""
        self._room: Any = None
        self._participant_identity: str = ""
        self._call_start: float = 0.0

        self._buffered_stt: dict | None = None
        self._buffered_llm: dict | None = None
        self._buffered_tts: dict | None = None

        self.transcript: list[dict] = []

    @property
    def enabled(self) -> bool:
        return bool(self._url and self._key and self._run_id)

    def configure(
        self,
        *,
        run_id: str | None,
        workflow_id: str = "",
        room_id: str = "",
        room: Any = None,
        participant_identity: str = "",
    ) -> None:
        self._run_id = run_id
        self._workflow_id = workflow_id
        self._room_id = room_id
        self._room = room
        self._participant_identity = participant_identity
        self._call_start = time.monotonic()

    async def update_run(self, **fields: Any) -> None:
        if fields:
            logger.info("Run update: %s", fields)
            await asyncio.to_thread(self._update_run_sync, fields)

    async def record_message(self, role: str, text: str) -> None:
        logger.info("Transcript [%s]: %s", role, text[:120])
        ts = datetime.now(timezone.utc).isoformat()
        timestamp_ms = self._elapsed_ms()

        await self._publish_to_room(
            {
                "event": "transcript",
                "role": role,
                "text": text,
                "timestamp": ts,
                "room_id": self._room_id,
                **(
                    {
                        "run_id": self._run_id,
                        "workflow_id": self._workflow_id,
                        "participant_identity": self._participant_identity,
                    }
                    if self._run_id
                    else {}
                ),
            }
        )

        result = self._drain_metrics_for(role)
        input_data = {
            "role": role,
            "text": text,
            "timestamp": ts,
            "timestamp_ms": timestamp_ms,
            "participantIdentity": self._participant_identity,
        }
        await asyncio.to_thread(self._insert_event_sync, "message", input_data, result)

    async def record_tool_call(
        self,
        tool_name: str,
        tool_args: Any,
        result_text: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        input_data = {
            "tool_name": tool_name,
            "tool_args": _try_parse_json(tool_args),
            "timestamp_ms": self._elapsed_ms(),
        }
        result_data: dict[str, Any] = {}
        if result_text is not None:
            result_data["output"] = result_text[:2000]
        if error is not None:
            result_data["error"] = error
        if duration_ms is not None:
            result_data["duration_ms"] = duration_ms
        if self._buffered_llm:
            result_data.update(self._buffered_llm)
            self._buffered_llm = None
        await asyncio.to_thread(self._insert_event_sync, "tool_call", input_data, result_data)

    async def record_lifecycle(self, event: str, **details: Any) -> None:
        input_data: dict[str, Any] = {
            "event": event,
            "timestamp_ms": self._elapsed_ms(),
            **details,
        }
        await asyncio.to_thread(self._insert_event_sync, "lifecycle", input_data)

    async def publish_data_message(
        self,
        topic: str,
        payload: dict[str, Any],
        lifecycle_event: str | None = None,
    ) -> None:
        """Publish a data payload to room and optionally persist lifecycle metadata."""
        await self._publish_to_room(payload, topic=topic)
        if lifecycle_event:
            await self.record_lifecycle(
                lifecycle_event,
                topic=topic,
                payload=payload,
            )

    async def record_sip_error(self, error: str, sip_code: int | str | None = None) -> None:
        input_data: dict[str, Any] = {
            "error": error,
            "timestamp_ms": self._elapsed_ms(),
        }
        if sip_code is not None:
            try:
                input_data["sip_code"] = int(sip_code)
            except (ValueError, TypeError):
                input_data["sip_code"] = str(sip_code)
        await asyncio.to_thread(self._insert_event_sync, "sip_error", input_data)

    def attach_to_session(self, session: Any) -> None:
        @session.on("metrics_collected")
        def _on_metrics(event: Any) -> None:
            self._buffer_metrics(event.metrics)

        @session.on("function_tools_executed")
        def _on_tools_executed(event: Any) -> None:
            for fn_call, fn_output in event.zipped():
                result_text = (
                    getattr(fn_output, "result", None)
                    or getattr(fn_output, "content", None)
                    or getattr(fn_output, "output", None)
                )
                error = getattr(fn_output, "error", None)
                tool_args = getattr(fn_call, "arguments", "{}")
                asyncio.create_task(
                    self.record_tool_call(
                        tool_name=fn_call.name,
                        tool_args=tool_args,
                        result_text=result_text,
                        error=error,
                    )
                )

        @session.on("user_input_transcribed")
        def _on_user_input(event: Any) -> None:
            try:
                if event.is_final and event.transcript:
                    ts = datetime.now(timezone.utc).isoformat()
                    self.transcript.append({"role": "user", "text": event.transcript, "timestamp": ts})
                    asyncio.create_task(self.record_message("user", event.transcript))
            except Exception as e:
                logger.warning("Failed to capture user transcript: %s", e)

        @session.on("conversation_item_added")
        def _on_conversation_item(event: Any) -> None:
            try:
                item = event.item
                role = getattr(item, "role", "unknown")
                if role == "user":
                    return

                content = ""
                if hasattr(item, "text_content") and item.text_content:
                    content = item.text_content
                elif hasattr(item, "content"):
                    parts = []
                    for part in item.content:
                        if isinstance(part, str):
                            parts.append(part)
                        elif hasattr(part, "transcript") and part.transcript:
                            parts.append(part.transcript)
                        elif hasattr(part, "text"):
                            parts.append(part.text)
                    content = " ".join(parts)

                if content:
                    ts = datetime.now(timezone.utc).isoformat()
                    self.transcript.append({"role": role, "text": content, "timestamp": ts})
                    asyncio.create_task(self.record_message(role, content))
            except Exception as e:
                logger.warning("Failed to capture transcript item: %s", e)

    def _buffer_metrics(self, metrics: Any) -> None:
        try:
            mtype = getattr(metrics, "type", "")
            if mtype == "stt_metrics":
                self._buffered_stt = {
                    "stt_duration_ms": round(getattr(metrics, "duration", 0) * 1000),
                    "stt_audio_duration_ms": round(getattr(metrics, "audio_duration", 0) * 1000),
                }
            elif mtype == "llm_metrics":
                self._buffered_llm = {
                    "llm_ttft_ms": round(getattr(metrics, "ttft", 0) * 1000),
                    "llm_duration_ms": round(getattr(metrics, "duration", 0) * 1000),
                    "llm_tokens": getattr(metrics, "total_tokens", 0),
                }
            elif mtype == "tts_metrics":
                self._buffered_tts = {
                    "tts_ttfb_ms": round(getattr(metrics, "ttfb", 0) * 1000),
                    "tts_duration_ms": round(getattr(metrics, "duration", 0) * 1000),
                }
        except Exception as e:
            logger.warning("Failed to buffer metrics: %s", e)

    def _drain_metrics_for(self, role: str) -> dict:
        result: dict[str, Any] = {}
        if role == "user" and self._buffered_stt:
            result.update(self._buffered_stt)
            self._buffered_stt = None
        elif role == "assistant":
            if self._buffered_llm:
                result.update(self._buffered_llm)
                self._buffered_llm = None
            if self._buffered_tts:
                result.update(self._buffered_tts)
                self._buffered_tts = None
        return result

    async def _publish_to_room(self, payload: dict, topic: str = TRANSCRIPT_TOPIC) -> None:
        if self._room is None:
            return
        try:
            local = getattr(self._room, "local_participant", None)
            if local is not None:
                data = json.dumps(payload).encode("utf-8")
                await local.publish_data(data, topic=topic)
        except Exception as e:
            logger.warning("Failed to publish to room: %s", e)

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }

    @staticmethod
    def _format_ts(iso: str) -> str:
        if not iso:
            return ""
        s = iso.replace("T", " ").replace("Z", "+00")
        if "+" not in s:
            s += "+00"
        return s

    def _insert_event_sync(self, event_name: str, input_data: dict, result_data: dict | None = None) -> None:
        if not self.enabled:
            return
        ts = datetime.now(timezone.utc).isoformat()
        row = {
            "id": str(uuid.uuid4()),
            "run_id": self._run_id,
            "workflow_id": self._workflow_id,
            "event_name": event_name,
            "input": input_data,
            "result": result_data or {},
            "start_timestamp": self._format_ts(ts),
            "end_timestamp": None,
            "attempts": 0,
        }
        try:
            resp = requests.post(
                f"{self._url}/rest/v1/durable_agent_run_event",
                json=row,
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
        except requests.HTTPError as e:
            body = ""
            if e.response is not None:
                body = e.response.text[:500]
            logger.warning("Failed to insert %s event: %s %s", event_name, e, body)
        except Exception as e:
            logger.warning("Failed to insert %s event: %s", event_name, e)

    def _update_run_sync(self, fields: dict) -> None:
        if not self._run_id or not self._url or not self._key:
            return
        params = f"run_id=eq.{self._run_id}"
        if self._workflow_id:
            params += f"&workflow_id=eq.{self._workflow_id}"
        try:
            resp = requests.patch(
                f"{self._url}/rest/v1/durable_agent_run?{params}",
                json=fields,
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
        except requests.HTTPError as e:
            body = ""
            if e.response is not None:
                body = e.response.text[:500]
            logger.warning("Failed to update run: %s %s", e, body)
        except Exception as e:
            logger.warning("Failed to update run: %s", e)

    def _elapsed_ms(self) -> int:
        if not self._call_start:
            return 0
        return int((time.monotonic() - self._call_start) * 1000)


def _try_parse_json(s: Any) -> Any:
    if isinstance(s, dict):
        return s
    if isinstance(s, str):
        try:
            return json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return {"raw": s}
    return {}
