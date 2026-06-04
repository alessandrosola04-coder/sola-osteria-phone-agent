import json
import os
import re
import smtplib
import uuid
from datetime import date as date_cls
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/New_York")
RESERVATION_REQUESTS_PATH = Path("data/reservation_requests.jsonl")

# Orari del ristorante, caricati dai dati per il controllo dei giorni di chiusura
def _load_restaurant_hours() -> dict:
    try:
        cfg_path = os.getenv("RESTAURANT_CONFIG", "data/restaurant.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f).get("hours", {})
    except Exception:
        return {}

RESTAURANT_HOURS = _load_restaurant_hours()


def check_hours(config: dict, day: str | None = None) -> str:
    hours = config["hours"]
    if day:
        day_key = normalize_day(day)
        value = hours.get(day_key)
        if value:
            return f"Orari per {day}: {value}"
        return "I could not find that day. I can check Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, or Sunday."

    today_key = datetime.now().strftime("%A").lower()
    english_to_config = {
        "monday": "monday",
        "tuesday": "tuesday",
        "wednesday": "wednesday",
        "thursday": "thursday",
        "friday": "friday",
        "saturday": "saturday",
        "sunday": "sunday",
    }
    key = english_to_config[today_key]
    return f"Oggi: {hours[key]}"


def get_menu(config: dict, category: str | None = None) -> str:
    categories = config.get("menu_categories", {})
    if category:
        key = normalize_category(category)
        items = categories.get(key)
        if not items:
            return "I could not find that menu category. I can discuss antipasti, pasta, secondi, dessert, or highlights."
        return format_menu_items(key, items)

    highlights = config.get("menu_highlights", [])
    return "Popular menu highlights include: " + ", ".join(highlights)


def get_restaurant_info(config: dict, topic: str) -> str:
    key = topic.strip().lower().replace(" ", "_")
    common_answers = config.get("common_answers", {})
    policies = config.get("policies", {})
    services = config.get("services", {})

    topic_map = {
        "address": config.get("address", ""),
        "location": config.get("address", ""),
        "phone": config.get("phone", ""),
        "email": config.get("email", ""),
        "chef": config.get("chef", {}).get("notes", ""),
        "reservations": services.get("reservations", policies.get("reservations", "")),
        "reservation": services.get("reservations", policies.get("reservations", "")),
        "takeout": services.get("takeout", policies.get("takeaway", "")),
        "take_out": services.get("takeout", policies.get("takeaway", "")),
        "delivery": services.get("delivery", policies.get("delivery", "")),
        "byob": "Sola Osteria is listed as BYOB. For detailed alcohol questions, staff should confirm.",
        "wine": policies.get("wine", ""),
        "large_parties": policies.get("large_parties", ""),
        "private_events": services.get("private_events", ""),
        "allergens": policies.get("allergens", ""),
        "allergies": policies.get("allergens", ""),
    }

    if key in topic_map and topic_map[key]:
        return str(topic_map[key])
    if key in common_answers:
        return str(common_answers[key])
    return "I do not have that detail confirmed from the restaurant data. I can take your name and number so staff can follow up."


def request_human_transfer(reason: str, caller_name: str | None = None, caller_phone: str | None = None) -> dict:
    staff_phone = os.getenv("STAFF_TRANSFER_PHONE", "").strip()
    if staff_phone and live_transfer_configured():
        return {
            "transfer_to_staff": True,
            "staff_phone_configured": True,
            "live_transfer_configured": True,
            "reason": reason,
            "caller_name": caller_name or "",
            "caller_phone": caller_phone or "",
            "assistant_next_step": "Tell the caller: One moment, I’ll connect you with our team now.",
        }

    return {
        "transfer_to_staff": False,
        "staff_phone_configured": bool(staff_phone),
        "live_transfer_configured": False,
        "reason": reason,
        "caller_name": caller_name or "",
        "caller_phone": caller_phone or "",
        "assistant_next_step": "Tell the caller that staff transfer is not available right now, then collect their name, phone number, and a short message for the restaurant team.",
    }


def live_transfer_configured() -> bool:
    required_env = ("STAFF_TRANSFER_PHONE", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")
    return all(os.getenv(name, "").strip() for name in required_env)


def resolve_reservation_date(date_text: str) -> dict:
    parsed = parse_reservation_date(date_text)
    if not parsed["resolved"]:
        return {
            "resolved": False,
            "date_input": date_text,
            "assistant_next_step": "Ask the caller for the reservation date again, using a specific weekday and month/day if possible.",
        }

    response = {
        "resolved": True,
        "date_input": date_text,
        "normalized_date": parsed["date"].isoformat(),
        "display_date": format_display_date(parsed["date"]),
        "weekday": parsed["date"].strftime("%A"),
        "confidence": parsed["confidence"],
        "requires_confirmation": False,
    }
    # Nessuna conferma intermedia: la data risolta si usa direttamente.
    # La conferma avviene nel riepilogo finale della prenotazione.
    response["assistant_next_step"] = f"Use {response['display_date']} as the reservation date. Do not ask the caller to confirm the date now."
    return response


def format_menu_items(category: str, items: list[dict]) -> str:
    label = category.replace("_", " ").title()
    formatted = []
    for item in items:
        price = f" ({item['price']})" if item.get("price") else ""
        formatted.append(f"{item['name']}{price}: {item.get('description', '')}")
    return f"{label}: " + " | ".join(formatted)


def normalize_category(category: str) -> str:
    value = category.strip().lower().replace(" ", "_")
    aliases = {
        "appetizers": "antipasti",
        "appetizer": "antipasti",
        "starter": "antipasti",
        "starters": "antipasti",
        "antipasto": "antipasti",
        "primi": "pasta",
        "pastas": "pasta",
        "main": "secondi",
        "mains": "secondi",
        "second": "secondi",
        "seconds": "secondi",
        "entree": "secondi",
        "entrees": "secondi",
        "desserts": "dessert",
        "dolci": "dessert",
    }
    return aliases.get(value, value)


def normalize_day(day: str) -> str:
    value = day.strip().lower()
    aliases = {
        "monday": "monday",
        "mon": "monday",
        "lunedi": "monday",
        "lunedì": "monday",
        "tuesday": "tuesday",
        "tue": "tuesday",
        "martedi": "tuesday",
        "martedì": "tuesday",
        "wednesday": "wednesday",
        "wed": "wednesday",
        "mercoledi": "wednesday",
        "mercoledì": "wednesday",
        "thursday": "thursday",
        "thu": "thursday",
        "giovedi": "thursday",
        "giovedì": "thursday",
        "friday": "friday",
        "fri": "friday",
        "venerdi": "friday",
        "venerdì": "friday",
        "saturday": "saturday",
        "sat": "saturday",
        "sabato": "saturday",
        "sunday": "sunday",
        "sun": "sunday",
        "domenica": "sunday",
    }
    return aliases.get(value, value)


def parse_reservation_date(date_text: str, today: date_cls | None = None) -> dict:
    today = today or datetime.now(LOCAL_TZ).date()
    text = normalize_text(date_text)

    if not text:
        return {"resolved": False}

    if text in {"today", "tonight", "oggi", "stasera"}:
        return {"resolved": True, "date": today, "confidence": "high"}
    if text in {"tomorrow", "domani"}:
        return {"resolved": True, "date": today + timedelta(days=1), "confidence": "high"}

    explicit = parse_explicit_date(text, today)
    if explicit:
        return {"resolved": True, "date": explicit, "confidence": "high"}

    weekday_match = parse_weekday_phrase(text, today)
    if weekday_match:
        return weekday_match

    return {"resolved": False}


def parse_explicit_date(text: str, today: date_cls) -> date_cls | None:
    iso_match = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)
    if iso_match:
        return date_cls(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))

    slash_match = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
    if slash_match:
        month = int(slash_match.group(1))
        day = int(slash_match.group(2))
        year = parse_year(slash_match.group(3), today.year)
        candidate = date_cls(year, month, day)
        return roll_forward_if_past(candidate, today, slash_match.group(3) is None)

    month_names = {
        "january": 1,
        "jan": 1,
        "february": 2,
        "feb": 2,
        "march": 3,
        "mar": 3,
        "april": 4,
        "apr": 4,
        "may": 5,
        "june": 6,
        "jun": 6,
        "july": 7,
        "jul": 7,
        "august": 8,
        "aug": 8,
        "september": 9,
        "sep": 9,
        "october": 10,
        "oct": 10,
        "november": 11,
        "nov": 11,
        "december": 12,
        "dec": 12,
    }
    month_pattern = "|".join(month_names)
    month_first = re.search(rf"\b({month_pattern})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b", text)
    if month_first:
        year = int(month_first.group(3) or today.year)
        candidate = date_cls(year, month_names[month_first.group(1)], int(month_first.group(2)))
        return roll_forward_if_past(candidate, today, month_first.group(3) is None)

    day_first = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})(?:,?\s+(20\d{{2}}))?\b", text)
    if day_first:
        year = int(day_first.group(3) or today.year)
        candidate = date_cls(year, month_names[day_first.group(2)], int(day_first.group(1)))
        return roll_forward_if_past(candidate, today, day_first.group(3) is None)

    return None


def parse_weekday_phrase(text: str, today: date_cls) -> dict | None:
    weekdays = {
        "monday": 0,
        "mon": 0,
        "lunedi": 0,
        "lunedi": 0,
        "tuesday": 1,
        "tue": 1,
        "martedi": 1,
        "wednesday": 2,
        "wed": 2,
        "mercoledi": 2,
        "thursday": 3,
        "thu": 3,
        "giovedi": 3,
        "friday": 4,
        "fri": 4,
        "venerdi": 4,
        "saturday": 5,
        "sat": 5,
        "sabato": 5,
        "sunday": 6,
        "sun": 6,
        "domenica": 6,
    }
    for word, target_weekday in weekdays.items():
        if re.search(rf"\b{word}\b", text):
            days_until = (target_weekday - today.weekday()) % 7
            has_this = re.search(r"\b(this|questo|questa)\b", text) is not None
            has_next = re.search(r"\b(next|prossimo|prossima)\b", text) is not None

            if has_next:
                # "next Saturday" = il sabato della settimana prossima (+7 dal piu vicino)
                nearest = today + timedelta(days=days_until or 7)
                candidate = nearest + timedelta(days=7)
                return {"resolved": True, "date": candidate, "confidence": "high"}
            # "Saturday" o "this Saturday" = il primo sabato piu vicino
            candidate = today + timedelta(days=days_until or 7)
            return {"resolved": True, "date": candidate, "confidence": "high"}
    return None


def parse_year(raw_year: str | None, current_year: int) -> int:
    if not raw_year:
        return current_year
    year = int(raw_year)
    if year < 100:
        return 2000 + year
    return year


def roll_forward_if_past(candidate: date_cls, today: date_cls, year_was_omitted: bool) -> date_cls:
    if year_was_omitted and candidate < today:
        return date_cls(candidate.year + 1, candidate.month, candidate.day)
    return candidate


def normalize_text(value: str) -> str:
    replacements = str.maketrans({"ì": "i", "í": "i", "è": "e", "é": "e", "à": "a", "ò": "o", "ù": "u"})
    return value.strip().lower().translate(replacements)


def format_display_date(value: date_cls) -> str:
    return value.strftime("%A, %B %-d, %Y")


def create_reservation_request(
    party_size: int,
    name: str = "",
    date: str = "",
    time: str = "",
    phone: str = "",
    date_confirmed: bool = False,
    notes: str | None = None,
) -> dict:
    if party_size <= 0:
        return {
            "status": "needs_reservation_details",
            "transfer_to_staff": False,
            "missing_fields": ["party_size"],
            "assistant_next_step": "Ask the caller how many people the reservation is for.",
        }

    if party_size >= 10:
        if not live_transfer_configured():
            return {
                "status": "requires_staff_for_large_party",
                "transfer_to_staff": False,
                "live_transfer_configured": False,
                "party_size": party_size,
                "reason": "large party of 10 or more",
                "caller_name": name,
                "caller_phone": phone,
                "assistant_next_step": "Tell the caller that large parties need the owner, but live transfer is not available right now. Collect their name, phone number, requested date/time, and tell them the team will follow up.",
            }
        return {
            "status": "requires_staff_for_large_party",
            "transfer_to_staff": True,
            "live_transfer_configured": True,
            "party_size": party_size,
            "reason": "large party of 10 or more",
            "caller_name": name,
            "caller_phone": phone,
            "transfer_message": "I'm allowed to take reservations for up to 9 people. I'll transfer the call directly to the owner to finalize your reservation. Thank you, and have a great day.",
            "assistant_next_step": "Say: I'm allowed to take reservations for up to 9 people. I'll transfer the call directly to the owner to finalize your reservation. Thank you, and have a great day.",
        }

    missing_fields = [
        field
        for field, value in {
            "name": name,
            "date": date,
            "time": time,
            "phone": phone,
        }.items()
        if not str(value).strip()
    ]
    if missing_fields:
        return {
            "status": "needs_reservation_details",
            "transfer_to_staff": False,
            "missing_fields": missing_fields,
            "assistant_next_step": f"Ask the caller for the next missing reservation detail: {missing_fields[0]}.",
        }

    parsed = resolve_reservation_date(date)
    if not parsed["resolved"]:
        return {
            "status": "needs_date_clarification",
            "date_input": date,
            "assistant_next_step": parsed["assistant_next_step"],
        }

    if parsed["requires_confirmation"] and not date_confirmed:
        return {
            "status": "needs_date_confirmation",
            "date_input": date,
            "normalized_date": parsed["normalized_date"],
            "display_date": parsed["display_date"],
            "assistant_next_step": parsed["assistant_next_step"],
        }

    # --- Controllo giorno di chiusura (a prova di errore) ---
    try:
        from app.tools import load_restaurant_config_hours  # noqa
    except Exception:
        pass
    _hours = (RESTAURANT_HOURS or {})
    _weekday = parsed["normalized_date"]
    try:
        _dt = datetime.strptime(parsed["normalized_date"], "%Y-%m-%d")
        _weekday = _dt.strftime("%A").lower()
    except Exception:
        _weekday = ""
    _day_hours = str(_hours.get(_weekday, "")).strip().lower()
    if _weekday and (_day_hours == "closed" or _day_hours == ""):
        return {
            "status": "closed_day",
            "transfer_to_staff": False,
            "display_date": parsed["display_date"],
            "assistant_next_step": (
                f"Politely tell the caller the restaurant is closed on {_weekday.capitalize()} "
                f"and we cannot take a reservation for that day. Offer to book another day instead. "
                f"Do NOT save this reservation."
            ),
        }

    request = {
        "id": str(uuid.uuid4()),
        "status": "pending_staff_confirmation",
        "created_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
        "name": name,
        "date_input": date,
        "normalized_date": parsed["normalized_date"],
        "display_date": parsed["display_date"],
        "time": time,
        "party_size": party_size,
        "phone": phone,
        "notes": notes or "",
    }
    save_reservation_request(request)
    notify_staff_about_reservation(request)
    return request


def save_reservation_request(request: dict) -> None:
    RESERVATION_REQUESTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESERVATION_REQUESTS_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(request, ensure_ascii=False) + "\n")


def list_reservation_requests(limit: int = 50) -> list[dict]:
    if not RESERVATION_REQUESTS_PATH.exists():
        return []

    requests = []
    with RESERVATION_REQUESTS_PATH.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line:
                requests.append(json.loads(line))
    return list(reversed(requests[-limit:]))


def notify_staff_about_reservation(request: dict) -> None:
    recipient = os.getenv("RESERVATION_NOTIFY_EMAIL")
    smtp_host = os.getenv("SMTP_HOST")
    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    from_email = os.getenv("SMTP_FROM_EMAIL") or smtp_username

    if not all([recipient, smtp_host, smtp_username, smtp_password, from_email]):
        request["notification_status"] = "email_not_configured"
        return

    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    message = EmailMessage()
    message["Subject"] = f"New Sola Osteria reservation request: {request['display_date']} at {request['time']}"
    message["From"] = from_email
    message["To"] = recipient
    message.set_content(format_reservation_email(request))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(smtp_username, smtp_password)
            smtp.send_message(message)
        request["notification_status"] = "email_sent"
    except Exception as exc:
        request["notification_status"] = f"email_failed: {type(exc).__name__}"


def format_reservation_email(request: dict) -> str:
    return f"""New reservation request from Isabel

Status: {request['status']}
Name: {request['name']}
Phone: {request['phone']}
Date: {request['display_date']} ({request['normalized_date']})
Time: {request['time']}
Party size: {request['party_size']}
Notes: {request.get('notes') or '-'}

Important: Isabel did not confirm availability. Staff must confirm this reservation manually.
Request ID: {request['id']}
Created at: {request['created_at']}
"""
