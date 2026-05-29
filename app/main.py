import asyncio
import contextlib
import json
import os
from typing import Any

import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi import HTTPException
from fastapi.responses import Response

from app.config import build_agent_instructions, load_restaurant_config
from app.tools import (
    check_hours,
    create_reservation_request,
    get_menu,
    get_restaurant_info,
    list_reservation_requests,
    request_human_transfer,
    resolve_reservation_date,
)

load_dotenv()

app = FastAPI(title="Restaurant Phone Agent")
restaurant = load_restaurant_config()

LARGE_PARTY_TRANSFER_MESSAGE = (
    "I'm allowed to take reservations for up to 9 people. "
    "I'll transfer the call directly to the owner to finalize your reservation. "
    "Thank you, and have a great day."
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/reservations")
async def reservations(request: Request, limit: int = 50) -> dict[str, Any]:
    configured_token = os.getenv("STAFF_ACCESS_TOKEN")
    provided_token = request.query_params.get("token") or request.headers.get("x-staff-access-token")
    if configured_token and provided_token != configured_token:
        raise HTTPException(status_code=401, detail="Missing or invalid staff access token")
    items = list_reservation_requests(limit=max(1, min(limit, 200)))
    return {"count": len(items), "items": items}


@app.post("/twilio/inbound")
async def twilio_inbound(request: Request) -> Response:
    public_base_url = get_public_base_url(request)
    stream_url = f"{public_base_url.replace('https://', 'wss://').replace('http://', 'ws://')}/twilio/media"
    staff_transfer_phone = os.getenv("STAFF_TRANSFER_PHONE", "").strip()

    if staff_transfer_phone:
        transfer_twiml = f"  <Dial>{staff_transfer_phone}</Dial>"
    else:
        transfer_twiml = "  <Say>Our team is not available right now. Please call back during business hours.</Say>"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{stream_url}" />
  </Connect>
{transfer_twiml}
</Response>"""
    return Response(content=twiml, media_type="application/xml")


def get_public_base_url(request: Request) -> str:
    configured = os.getenv("PUBLIC_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    forwarded_proto = request.headers.get("x-forwarded-proto", "https").split(",")[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}".rstrip("/")
    return str(request.base_url).rstrip("/")


@app.websocket("/twilio/media")
async def twilio_media(websocket: WebSocket) -> None:
    await websocket.accept()

    openai_key = os.environ["OPENAI_API_KEY"]
    realtime_url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
    stream_sid: str | None = None
    transfer_requested = False
    call_started = False
    closing_for_transfer = False
    transfer_audio_played = asyncio.Event()

    async with websockets.connect(
        realtime_url,
        additional_headers={
            "Authorization": f"Bearer {openai_key}",
            "OpenAI-Beta": "realtime=v1",
        },
    ) as openai_ws:
        await configure_realtime_session(openai_ws)

        async def receive_from_twilio() -> None:
            nonlocal stream_sid, call_started, closing_for_transfer
            try:
                async for raw_message in websocket.iter_text():
                    message = json.loads(raw_message)
                    event = message.get("event")

                    if event == "start":
                        stream_sid = message["start"]["streamSid"]
                        if not call_started:
                            call_started = True
                            # Trigger greeting once only
                            await openai_ws.send(json.dumps({
                                "type": "response.create",
                                "response": {
                                    "modalities": ["audio"],
                                    "instructions": (
                                        "Greet the caller by saying exactly: "
                                        "'Sola Osteria, this is Isabel the AI receptionist, how can I help you?' "
                                        "Say nothing else. Wait for the caller to respond."
                                    ),
                                }
                            }))
                    elif event == "mark":
                        mark_name = message.get("mark", {}).get("name")
                        if mark_name == "transfer_complete":
                            transfer_audio_played.set()
                    elif event == "media":
                        if closing_for_transfer:
                            continue
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": message["media"]["payload"],
                        }))
                    elif event == "stop":
                        with contextlib.suppress(Exception):
                            await openai_ws.close()
                        break
            except WebSocketDisconnect:
                with contextlib.suppress(Exception):
                    await openai_ws.close()
            except websockets.exceptions.ConnectionClosed:
                return

        async def send_to_twilio() -> None:
            nonlocal transfer_requested, closing_for_transfer
            async for raw_message in openai_ws:
                event = json.loads(raw_message)
                event_type = event.get("type")

                if event_type == "response.audio.delta" and stream_sid:
                    await websocket.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": event["delta"]},
                    })
                elif event_type == "response.function_call_arguments.done":
                    result = await handle_tool_call(openai_ws, event)
                    if result.get("transfer_to_staff"):
                        transfer_requested = True
                elif event_type == "response.done" and transfer_requested:
                    closing_for_transfer = True
                    if stream_sid:
                        await websocket.send_json({
                            "event": "mark",
                            "streamSid": stream_sid,
                            "mark": {"name": "transfer_complete"},
                        })
                        try:
                            await asyncio.wait_for(transfer_audio_played.wait(), timeout=12)
                        except asyncio.TimeoutError:
                            pass
                    await asyncio.sleep(0.5)
                    with contextlib.suppress(Exception):
                        await openai_ws.close()
                    with contextlib.suppress(Exception):
                        await websocket.close()
                    break
                elif event_type == "error":
                    print("OpenAI realtime error:", json.dumps(event))

        await asyncio.gather(receive_from_twilio(), send_to_twilio())


async def configure_realtime_session(openai_ws: Any) -> None:
    await openai_ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "modalities": ["audio", "text"],
            "instructions": build_agent_instructions(restaurant),
            "voice": "alloy",
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.6,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 700,
            },
            "tools": [
                {
                    "type": "function",
                    "name": "check_hours",
                    "description": "Check restaurant opening hours for a specific day or today.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "day": {
                                "type": "string",
                                "description": "Day of the week, e.g. monday, saturday. Leave empty for today."
                            }
                        },
                    },
                },
                {
                    "type": "function",
                    "name": "get_menu",
                    "description": "Returns menu highlights or items from a specific category.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "description": "Optional: antipasti, pasta, secondi, dessert."
                            }
                        },
                    },
                },
                {
                    "type": "function",
                    "name": "get_restaurant_info",
                    "description": "Answers questions about address, chef, reservations, takeout, delivery, BYOB, allergies, large parties.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "topic": {
                                "type": "string",
                                "description": "Topic the caller is asking about."
                            }
                        },
                        "required": ["topic"],
                    },
                },
                {
                    "type": "function",
                    "name": "resolve_reservation_date",
                    "description": "Converts relative dates like 'next Saturday' or 'tomorrow' into exact calendar dates.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "date_text": {
                                "type": "string",
                                "description": "Natural language date from caller."
                            }
                        },
                        "required": ["date_text"],
                    },
                },
                {
                    "type": "function",
                    "name": "create_reservation_request",
                    "description": "Records a reservation request after collecting all required info and confirming the exact date. For 10 or more people, triggers owner transfer instead.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "date": {"type": "string", "description": "Exact date like 2026-05-30"},
                            "time": {"type": "string"},
                            "party_size": {"type": "integer"},
                            "phone": {"type": "string"},
                            "date_confirmed": {"type": "boolean", "description": "True only after caller confirmed the exact date."},
                            "notes": {"type": "string"},
                        },
                        "required": ["name", "date", "time", "party_size", "phone", "date_confirmed"],
                    },
                },
                {
                    "type": "function",
                    "name": "request_human_transfer",
                    "description": "Transfer to staff ONLY when: caller explicitly asks for a person/manager/owner, caller is angry/upset, caller has complaint, caller asks about serious allergy or celiac safety, caller wants to change or cancel existing reservation, caller asks for real-time availability.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {
                                "type": "string",
                                "description": "Short reason: manager_request, complaint, allergy_concern, change_existing_reservation, caller_request."
                            },
                            "caller_name": {"type": "string"},
                            "caller_phone": {"type": "string"},
                        },
                        "required": ["reason"],
                    },
                },
            ],
            "tool_choice": "auto",
        }
    }))


async def handle_tool_call(openai_ws: Any, event: dict[str, Any]) -> dict[str, Any]:
    name = event.get("name")
    call_id = event.get("call_id")
    arguments = json.loads(event.get("arguments") or "{}")

    if name == "check_hours":
        result = check_hours(restaurant, arguments.get("day"))
    elif name == "get_menu":
        result = get_menu(restaurant, arguments.get("category"))
    elif name == "get_restaurant_info":
        result = get_restaurant_info(restaurant, arguments.get("topic", ""))
    elif name == "resolve_reservation_date":
        result = resolve_reservation_date(arguments.get("date_text", ""))
    elif name == "create_reservation_request":
        result = create_reservation_request(**arguments)
    elif name == "request_human_transfer":
        result = request_human_transfer(
            reason=arguments.get("reason", ""),
            caller_name=arguments.get("caller_name"),
            caller_phone=arguments.get("caller_phone"),
        )
    else:
        result = {"error": f"Unknown tool: {name}"}

    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(result, ensure_ascii=False),
        },
    }))

    if result.get("transfer_to_staff"):
        transfer_msg = result.get("transfer_message") or LARGE_PARTY_TRANSFER_MESSAGE
        await openai_ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "modalities": ["audio"],
                "instructions": f"Say exactly these words and nothing else: {transfer_msg}",
            },
        }))
    else:
        await openai_ws.send(json.dumps({"type": "response.create"}))

    return result


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)
