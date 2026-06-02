import asyncio
import base64
import contextlib
import json
import os
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

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


@app.get("/keep-alive")
async def keep_alive() -> dict[str, str]:
    return {"status": "awake"}


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

    # IMPORTANTE: nessun <Dial> qui come fallback.
    # Il trasferimento avviene SOLO via WebSocket quando Isabel lo decide.
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{stream_url}">
      <Parameter name="publicBaseUrl" value="{public_base_url}" />
    </Stream>
  </Connect>
  <Say>Sorry, I am having trouble connecting the assistant right now. Please call the restaurant team directly.</Say>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/twilio/transfer")
async def twilio_transfer(request: Request) -> Response:
    """Endpoint chiamato via Twilio REST API per trasferire la chiamata live."""
    staff_phone = os.getenv("STAFF_TRANSFER_PHONE", "").strip()
    if staff_phone:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial>{staff_phone}</Dial>
</Response>"""
    else:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>Our team is not available right now. Please call back during business hours.</Say>
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


async def redirect_twilio_call_to_transfer(call_sid: str | None, public_base_url: str | None = None) -> bool:
    """Redirect an active Twilio call to the transfer TwiML endpoint."""
    if not call_sid:
        print("Twilio transfer skipped: missing callSid")
        return False

    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    base_url = (public_base_url or os.getenv("PUBLIC_BASE_URL", "")).strip().rstrip("/")
    if not all([account_sid, auth_token, base_url]):
        print("Twilio transfer skipped: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, or PUBLIC_BASE_URL missing")
        return False

    transfer_url = f"{base_url}/twilio/transfer"
    api_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    body = urlencode({"Url": transfer_url, "Method": "POST"}).encode("utf-8")

    def _send_redirect() -> bool:
        request = UrlRequest(api_url, data=body, method="POST")

        credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
        request.add_header("Authorization", f"Basic {credentials}")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urlopen(request, timeout=10) as response:
                return 200 <= response.status < 300
        except URLError as exc:
            print(f"Twilio transfer failed: {exc}")
            return False

    return await asyncio.to_thread(_send_redirect)


@app.websocket("/twilio/media")
async def twilio_media(websocket: WebSocket) -> None:
    await websocket.accept()

    openai_key = os.environ["OPENAI_API_KEY"]
    realtime_model = os.getenv("REALTIME_MODEL", "gpt-realtime")
    realtime_url = f"wss://api.openai.com/v1/realtime?model={realtime_model}"
    stream_sid: str | None = None
    call_sid: str | None = None
    public_base_url: str | None = None
    transfer_requested = False
    call_started = False
    greeting_in_progress = False
    _caller_has_spoken = False
    closing_for_transfer = False
    transfer_audio_played = asyncio.Event()

    async with websockets.connect(
        realtime_url,
        additional_headers={"Authorization": f"Bearer {openai_key}"},
    ) as openai_ws:
        await configure_realtime_session(openai_ws)

        async def receive_from_twilio() -> None:
            nonlocal stream_sid, call_sid, public_base_url, call_started, closing_for_transfer, greeting_in_progress, _caller_has_spoken
            try:
                async for raw_message in websocket.iter_text():
                    message = json.loads(raw_message)
                    event = message.get("event")

                    if event == "start":
                        stream_sid = message["start"]["streamSid"]
                        call_sid = message["start"].get("callSid")
                        custom_parameters = message["start"].get("customParameters", {})
                        public_base_url = custom_parameters.get("publicBaseUrl") or None
                        if not call_started:
                            call_started = True
                            greeting_in_progress = True
                            await openai_ws.send(json.dumps({
                                "type": "response.create",
                                "response": {
                                    "output_modalities": ["audio"],
                                    "instructions": (
                                        "Say ONLY this exact greeting and then immediately stop and wait: "
                                        "'Sola Osteria, this is Isabel the AI receptionist, how can I help you?' "
                                        "Do NOT say anything else. Do NOT mention hours, reservations, or ask follow-up questions. "
                                        "After saying the greeting, remain completely silent until the caller speaks first."
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
                elif event_type == "response.output_audio.delta" and stream_sid:
                    await websocket.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": event["delta"]},
                    })
                elif event_type == "response.function_call_arguments.done":
                    result = await handle_tool_call(openai_ws, event, call_sid)
                    if result.get("transfer_to_staff"):
                        transfer_requested = True
                elif event_type == "response.done" and greeting_in_progress:
                    # La prima response.done e la fine del greeting.
                    # Da ora il VAD potrebbe generare una risposta a vuoto: la sopprimiamo
                    # cancellando qualsiasi risposta non richiesta finche il chiamante non parla.
                    greeting_in_progress = False
                elif event_type == "response.created" and greeting_in_progress is False and not _caller_has_spoken:
                    # Risposta generata senza che il chiamante abbia parlato: annullala
                    with contextlib.suppress(Exception):
                        await openai_ws.send(json.dumps({"type": "response.cancel"}))
                elif event_type == "input_audio_buffer.speech_started":
                    _caller_has_spoken = True
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
                    await redirect_twilio_call_to_transfer(call_sid, public_base_url)
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
            "type": "realtime",
            "instructions": build_agent_instructions(restaurant),
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.65,
                        "prefix_padding_ms": 500,
                        "silence_duration_ms": 700,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": "coral",
                },
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
                                "description": "Day of the week e.g. monday, saturday. Leave empty for today."
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
                    "description": "Answers questions about address, chef, reservations, takeout, delivery, BYOB, allergies.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "topic": {"type": "string"}
                        },
                        "required": ["topic"],
                    },
                },
                {
                    "type": "function",
                    "name": "resolve_reservation_date",
                    "description": "Converts relative dates like next Saturday or tomorrow into exact calendar dates.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "date_text": {"type": "string"}
                        },
                        "required": ["date_text"],
                    },
                },
                {
                    "type": "function",
                    "name": "create_reservation_request",
                    "description": "Records a reservation request after collecting all info and confirming the exact date. For 10 or more people triggers owner transfer automatically.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "date": {"type": "string"},
                            "time": {"type": "string"},
                            "party_size": {"type": "integer"},
                            "phone": {"type": "string"},
                            "date_confirmed": {"type": "boolean"},
                            "notes": {"type": "string"},
                        },
                        "required": ["party_size"],
                    },
                },
                {
                    "type": "function",
                    "name": "request_human_transfer",
                    "description": "Transfer to staff ONLY when: caller asks for manager/owner/person, caller is angry or has complaint, caller asks about serious allergy or celiac, caller wants to change or cancel existing reservation, caller asks real-time availability.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {"type": "string"},
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


async def handle_tool_call(openai_ws: Any, event: dict[str, Any], call_sid: str | None = None) -> dict[str, Any]:
    name = event.get("name")
    call_id = event.get("call_id")
    try:
        arguments = json.loads(event.get("arguments") or "{}")
    except json.JSONDecodeError:
        arguments = {}

    if name == "check_hours":
        result = check_hours(restaurant, arguments.get("day"))
    elif name == "get_menu":
        result = get_menu(restaurant, arguments.get("category"))
    elif name == "get_restaurant_info":
        result = get_restaurant_info(restaurant, arguments.get("topic", ""))
    elif name == "resolve_reservation_date":
        result = resolve_reservation_date(arguments.get("date_text", ""))
    elif name == "create_reservation_request":
        try:
            party_size = int(arguments.get("party_size") or 0)
        except (TypeError, ValueError):
            party_size = 0
        result = create_reservation_request(
            party_size=party_size,
            name=arguments.get("name", ""),
            date=arguments.get("date", ""),
            time=arguments.get("time", ""),
            phone=arguments.get("phone", ""),
            date_confirmed=bool(arguments.get("date_confirmed", False)),
            notes=arguments.get("notes"),
        )
    elif name == "request_human_transfer":
        result = request_human_transfer(
            reason=arguments.get("reason", ""),
            caller_name=arguments.get("caller_name"),
            caller_phone=arguments.get("caller_phone"),
        )
    else:
        result = {"error": f"Unknown tool: {name}"}

    if not isinstance(result, dict):
        result = {"message": str(result)}

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
                "output_modalities": ["audio"],
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
