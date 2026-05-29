# Handoff for Claude: Sola Osteria AI Phone Agent

## Project Goal

Build and deploy a stable AI phone receptionist for Sola Osteria that answers customer calls, handles common restaurant questions, takes normal reservation requests, and transfers calls to the owner when needed.

The local project folder is:

```text
/Users/alessandrino/Documents/New project/restaurant-phone-agent
```

The GitHub repo the user created is:

```text
https://github.com/alessandrosola04-coder/sola-osteria-phone-agent
```

The repo is currently empty on GitHub or not successfully pushed yet. The local repo has one commit ready:

```text
81d52d4 Initial Sola Osteria phone agent
```

The user is not comfortable with Git/GitHub. Give very explicit click-by-click instructions or use an authenticated connector if available.

## Important Context

The user speaks Italian and wants guidance in Italian. They are building this for their father's restaurant, Sola Osteria.

The current local app works as a FastAPI + Twilio Media Streams + OpenAI Realtime phone agent.

Key phone behavior:

- Greets with:

```text
Sola Osteria, this is Isabel the AI receptionist, how can I help you?
```

- Speaks English by default.
- Switches to Italian if the caller speaks Italian.
- Answers restaurant questions.
- Handles reservation requests.
- Converts relative dates to exact calendar dates.
- For parties up to 9 people, it may collect a normal reservation request.
- For parties of 10 or more, it must transfer to the owner.
- It must never confirm real availability. Reservation requests are `pending_staff_confirmation`.

## Current Restaurant Data

Restaurant: Sola Osteria  
Address: 17 S Broad Street, Ridgewood, NJ 07450  
Phone: (201) 857-5100  
Email: ms@solaosteria.com  
Website: https://www.solaosteria.com/  
Owner/Chef: Massimo Sola  
Manager: Niccolo Sola  
Owner transfer phone configured locally: `+12019954521`

Restaurant hours in `data/restaurant.json`:

```json
{
  "monday": "12:15 PM - 9:00 PM",
  "tuesday": "Closed",
  "wednesday": "12:15 PM - 9:00 PM",
  "thursday": "12:15 PM - 10:00 PM",
  "friday": "12:15 PM - 10:00 PM",
  "saturday": "12:15 PM - 10:00 PM",
  "sunday": "2:00 PM - 8:00 PM"
}
```

The menu and policies are already encoded in:

```text
data/restaurant.json
```

## Local Files Created

Important files:

```text
app/main.py
app/config.py
app/tools.py
data/restaurant.json
docs/agent-contract.md
requirements.txt
Procfile
render.yaml
Dockerfile
fly.toml
README.md
.env.example
.gitignore
```

Ignored local secret/runtime files:

```text
.env
.venv/
.ngrok/
tools/
data/reservation_requests.jsonl
```

Do not commit `.env`. It contains the OpenAI key and the staff phone number.

## Current Local Environment

The project uses Python 3.12 in `.venv`.

Local install command:

```bash
cd "/Users/alessandrino/Documents/New project/restaurant-phone-agent"
.venv/bin/python -m pip install -r requirements.txt
```

Local run command:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8002
```

The app supports Render/Fly deployment.

## OpenAI

An OpenAI API key was created through the OpenAI Platform connector and saved locally in `.env` as:

```text
OPENAI_API_KEY=...
```

Do not reveal it.

Model:

```text
REALTIME_MODEL=gpt-realtime-mini
```

The user added $5 OpenAI credit. Earlier, insufficient quota was fixed by adding credit.

## Twilio

The user upgraded Twilio from trial to paid with $20. Auto-recharge was later disabled.

Twilio currently needs a stable webhook URL once deployed. During local testing, temporary ngrok/Cloudflare tunnels were painful because URLs kept changing.

The final target should be:

```text
https://STABLE-RENDER-OR-FLY-DOMAIN/twilio/inbound
```

Twilio method:

```text
POST
```

## Important Bug History

1. Old Realtime API beta shape caused:

```text
invalid_request_error.beta_api_shape_disabled
```

Fixed by using the GA Realtime session shape.

2. OpenAI quota caused:

```text
insufficient_quota
```

Fixed by adding OpenAI credit.

3. Temporary tunnel URLs kept expiring. User wants stable deployment.

4. Transfers for parties of 10+ were not reliable during live testing because:

- sometimes Twilio was still pointed at an old tunnel/server;
- the stream was being closed too quickly, cutting off Isabel's transfer phrase;
- the backend was strengthened to force transfer for 10+.

Current code attempts to wait for Twilio playback using a Twilio `mark` before closing the WebSocket, so Twilio can continue to `<Dial>`.

This may still need one final live test after stable deployment.

## Date Logic

Implemented in `app/tools.py`.

Rules:

- `Saturday` -> closest upcoming Saturday.
- `this Saturday` -> closest upcoming Saturday.
- `next Saturday` -> Saturday after the closest upcoming Saturday.
- `tomorrow` -> next day.
- `tonight` -> today.
- `August 15` -> August 15 in the current year unless already past.
- Explicit dates like `8/15/2026` and `2026-08-15` work.

As of the current date context, Friday, May 29, 2026:

```text
Saturday -> Saturday, May 30, 2026
next Saturday -> Saturday, June 6, 2026
August 15 -> Saturday, August 15, 2026
```

## Reservation Logic

Implemented in `create_reservation_request` in `app/tools.py`.

Current intended behavior:

- Party size 1-9:
  - create pending reservation request after exact date confirmation.
  - save to `data/reservation_requests.jsonl`.

- Party size 10+:
  - do not save normal reservation.
  - return:

```json
{
  "status": "requires_staff_for_large_party",
  "transfer_to_staff": true
}
```

  - Isabel should say exactly:

```text
I’m allowed to take reservations for up to 9 people. I’ll transfer the call directly to the owner to finalize your reservation. Thank you, and have a great day.
```

  - then transfer to:

```text
+12019954521
```

## Deployment Plan

Recommended path: Render Web Service.

User should deploy from GitHub after pushing the local project.

Render settings:

```text
Runtime: Python
Build Command: pip install -r requirements.txt
Start Command: uvicorn app.main:app --host 0.0.0.0 --port $PORT
Health Check Path: /health
```

Environment variables on Render:

```env
OPENAI_API_KEY=...
REALTIME_MODEL=gpt-realtime-mini
RESTAURANT_CONFIG=data/restaurant.json
STAFF_TRANSFER_PHONE=+12019954521
STAFF_ACCESS_TOKEN=<long random token>
RESERVATION_NOTIFY_EMAIL=ms@solaosteria.com
```

Do not set `PUBLIC_BASE_URL` unless needed. The app now derives the public host from request headers in `get_public_base_url`.

After Render deploy, test:

```text
https://RENDER-DOMAIN/health
```

Expected:

```json
{"status":"ok"}
```

Then set Twilio webhook:

```text
https://RENDER-DOMAIN/twilio/inbound
```

Method:

```text
POST
```

## Git/GitHub Status

The user created this GitHub repo:

```text
https://github.com/alessandrosola04-coder/sola-osteria-phone-agent
```

Local repo in `restaurant-phone-agent` has remote:

```text
origin https://github.com/alessandrosola04-coder/sola-osteria-phone-agent.git
```

But command-line push failed because GitHub no longer accepts password authentication:

```text
remote: Invalid username or token. Password authentication is not supported for Git operations.
fatal: Authentication failed
```

GitHub Desktop was opened and logged in. It shows:

- Current Repository: `sola-osteria-phone-agent`
- Current Branch: `main`
- No local changes
- Fetch origin button, but not Push origin

Next helper should guide the user very explicitly through GitHub Desktop or use another auth path.

Possible fallback:

1. In GitHub Desktop, go to `Repository -> Repository Settings -> Remote`.
2. Confirm remote URL:

```text
https://github.com/alessandrosola04-coder/sola-osteria-phone-agent.git
```

3. Use `Repository -> Push`.

If that remains confusing, easiest alternative:

- Create a ZIP of the project excluding `.env`, `.venv`, `.ngrok`, `tools`, `data/reservation_requests.jsonl`.
- User can upload files manually to GitHub web UI or upload ZIP contents if GitHub permits.

Better if connector available:

- Use GitHub connector to create/upload contents or push via API.

## What To Do Next

1. Get the project files into GitHub.
2. Deploy to Render with the env vars above.
3. Update Twilio webhook to the Render URL.
4. Run three calls:

Test A:

```text
What time are you open today?
```

Expected: fast answer.

Test B:

```text
I’d like to book a table for 9 people next Saturday at 8.
```

Expected: Isabel can collect normal reservation request after exact date confirmation.

Test C:

```text
I’d like to book a table for 10 people tonight at 8.
```

Expected: Isabel says the exact transfer phrase and transfers to owner.

If Test C still cuts off, inspect the Twilio Media Stream handling in `app/main.py`, especially the mark/close sequence around:

```text
transfer_audio_played
event == "mark"
event_type == "response.done" and transfer_requested
```

Potential improvement if needed:

- Instead of relying on `<Connect><Stream>` finishing and then `<Dial>`, use Twilio's REST API to redirect the live call to a `/twilio/transfer` endpoint when transfer is needed. That requires `CallSid` from Twilio's start event and Twilio credentials. This is likely the most production-grade transfer approach.

