import asyncio
import json
import os
import unittest
from unittest.mock import patch

from app.main import configure_realtime_session, get_public_base_url, redirect_twilio_call_to_transfer, twilio_inbound
from app.tools import create_reservation_request, request_human_transfer


class FakeRequest:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}
        self.base_url = "https://fallback.example.com/"


class PhoneAgentTests(unittest.TestCase):
    def tearDown(self) -> None:
        for name in (
            "PUBLIC_BASE_URL",
            "STAFF_TRANSFER_PHONE",
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
        ):
            os.environ.pop(name, None)

    def test_public_base_url_prefers_environment(self) -> None:
        os.environ["PUBLIC_BASE_URL"] = "https://sola.example.com/"
        self.assertEqual(get_public_base_url(FakeRequest()), "https://sola.example.com")

    def test_public_base_url_uses_forwarded_headers(self) -> None:
        request = FakeRequest({"x-forwarded-proto": "https", "x-forwarded-host": "agent.example.com"})
        self.assertEqual(get_public_base_url(request), "https://agent.example.com")

    def test_inbound_twiml_connects_media_stream_and_has_fallback(self) -> None:
        os.environ["PUBLIC_BASE_URL"] = "https://agent.example.com"
        response = asyncio.run(twilio_inbound(FakeRequest()))
        body = response.body.decode("utf-8")

        self.assertIn('<Stream url="wss://agent.example.com/twilio/media">', body)
        self.assertIn('name="publicBaseUrl" value="https://agent.example.com"', body)
        self.assertIn("<Say>", body)

    def test_realtime_session_uses_current_audio_shape(self) -> None:
        class FakeOpenAIWebSocket:
            def __init__(self) -> None:
                self.messages: list[str] = []

            async def send(self, message: str) -> None:
                self.messages.append(message)

        websocket = FakeOpenAIWebSocket()
        asyncio.run(configure_realtime_session(websocket))
        session_update = json.loads(websocket.messages[0])
        session = session_update["session"]

        self.assertEqual(session_update["type"], "session.update")
        self.assertEqual(session["type"], "realtime")
        self.assertEqual(session["audio"]["input"]["format"], {"type": "audio/pcmu"})
        self.assertEqual(session["audio"]["output"]["format"], {"type": "audio/pcmu"})
        self.assertEqual(session["output_modalities"], ["audio"])

    def test_large_party_does_not_transfer_when_live_transfer_is_unconfigured(self) -> None:
        os.environ["STAFF_TRANSFER_PHONE"] = "+12019954521"
        result = create_reservation_request(party_size=10)

        self.assertFalse(result["transfer_to_staff"])
        self.assertEqual(result["status"], "requires_staff_for_large_party")
        self.assertFalse(result["live_transfer_configured"])

    def test_large_party_transfers_when_live_transfer_is_configured(self) -> None:
        os.environ["STAFF_TRANSFER_PHONE"] = "+12019954521"
        os.environ["TWILIO_ACCOUNT_SID"] = "ACxxx"
        os.environ["TWILIO_AUTH_TOKEN"] = "secret"

        result = create_reservation_request(party_size=10)

        self.assertTrue(result["transfer_to_staff"])
        self.assertTrue(result["live_transfer_configured"])

    def test_small_party_missing_details_are_requested(self) -> None:
        result = create_reservation_request(party_size=2)

        self.assertEqual(result["status"], "needs_reservation_details")
        self.assertEqual(result["missing_fields"], ["name", "date", "time", "phone"])

    def test_human_transfer_requires_twilio_redirect_config(self) -> None:
        os.environ["STAFF_TRANSFER_PHONE"] = "+12019954521"

        result = request_human_transfer("caller asked for owner")

        self.assertFalse(result["transfer_to_staff"])
        self.assertTrue(result["staff_phone_configured"])
        self.assertFalse(result["live_transfer_configured"])

    def test_redirect_skips_without_call_sid(self) -> None:
        result = asyncio.run(redirect_twilio_call_to_transfer(None))

        self.assertFalse(result)

    def test_redirect_posts_to_twilio_when_configured(self) -> None:
        os.environ["TWILIO_ACCOUNT_SID"] = "ACxxx"
        os.environ["TWILIO_AUTH_TOKEN"] = "secret"
        os.environ["PUBLIC_BASE_URL"] = "https://agent.example.com"

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        with patch("app.main.urlopen", return_value=FakeResponse()) as mocked_urlopen:
            result = asyncio.run(redirect_twilio_call_to_transfer("CAxxx"))

        self.assertTrue(result)
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.twilio.com/2010-04-01/Accounts/ACxxx/Calls/CAxxx.json")
        self.assertIn(b"Url=https%3A%2F%2Fagent.example.com%2Ftwilio%2Ftransfer", request.data)


if __name__ == "__main__":
    unittest.main()
