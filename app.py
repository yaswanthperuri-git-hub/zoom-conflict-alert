import json
import os
import hmac
import hashlib
import httpx
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)

ZOOM_SECRET_TOKEN = os.environ.get("ZOOM_SECRET_TOKEN", "")
ZOOM_ACCOUNT_ID = os.environ.get("ZOOM_ACCOUNT_ID", "")
ZOOM_CLIENT_ID = os.environ.get("ZOOM_CLIENT_ID", "")
ZOOM_CLIENT_SECRET = os.environ.get("ZOOM_CLIENT_SECRET", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

SIMULIVE_TYPES = {9}
LIVE_WEBINAR_TYPES = {5, 6}
LIVE_MEETING_TYPES = {2, 3, 8}
IST_OFFSET = timedelta(hours=5, minutes=30)


def get_zoom_access_token():
    resp = httpx.post(
        "https://zoom.us/oauth/token",
        params={"grant_type": "account_credentials", "account_id": ZOOM_ACCOUNT_ID},
        auth=(ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET),
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_webinar_actual_type(token, webinar_id):
    """Get the real type of a webinar from individual API — list API is unreliable for Simulive."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = httpx.get(f"https://api.zoom.us/v2/webinars/{webinar_id}", headers=headers)
    if resp.status_code == 200:
        actual_type = resp.json().get("type", 5)
        print(f"  DEBUG webinar {webinar_id} actual type={actual_type}")
        return actual_type
    return 5  # default to Live if can't fetch


def get_host_email(token, event_id, event_type):
    headers = {"Authorization": f"Bearer {token}"}
    if event_type == "webinar.created":
        resp = httpx.get(f"https://api.zoom.us/v2/webinars/{event_id}", headers=headers)
    else:
        resp = httpx.get(f"https://api.zoom.us/v2/meetings/{event_id}", headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        return data.get("host_email", "") or data.get("host_id", "")
    return "Unknown"


def get_all_scheduled_events(token, host_email):
    headers = {"Authorization": f"Bearer {token}"}
    events = []
    now = datetime.now(timezone.utc)

    user_id = host_email if host_email and host_email.strip() else "me"

    # Fetch upcoming meetings
    resp = httpx.get(
        f"https://api.zoom.us/v2/users/{user_id}/meetings",
        headers=headers,
        params={"type": "upcoming", "page_size": 100},
    )
    if resp.status_code == 200:
        for m in resp.json().get("meetings", []):
            start_raw = m.get("start_time", "")
            if not start_raw or not m.get("duration"):
                continue
            start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            if start_dt < now:
                continue
            events.append({
                "id": m["id"],
                "topic": m["topic"],
                "type": "Meeting",
                "zoom_type": m.get("type", 0),
                "is_simulive": m.get("type", 0) in SIMULIVE_TYPES,
                "start": start_dt + IST_OFFSET,
                "duration_mins": m["duration"],
            })

    # Fetch upcoming webinars and verify each one's actual type
    next_page_token = ""
    while True:
        params = {"page_size": 100}
        if next_page_token:
            params["next_page_token"] = next_page_token
        resp = httpx.get(
            f"https://api.zoom.us/v2/users/{user_id}/webinars",
            headers=headers,
            params=params,
        )
        if resp.status_code != 200:
            break

        data = resp.json()

        for w in data.get("webinars", []):
            start_raw = w.get("start_time", "")
            if not start_raw or not w.get("duration"):
                continue
            start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            if start_dt < now:
                continue

            # Verify actual type from individual API
            actual_type = get_webinar_actual_type(token, w["id"])
            is_simulive = actual_type in SIMULIVE_TYPES

            events.append({
                "id": w["id"],
                "topic": w["topic"],
                "type": "Webinar",
                "zoom_type": actual_type,
                "is_simulive": is_simulive,
                "start": start_dt + IST_OFFSET,
                "duration_mins": w["duration"],
            })

        next_page_token = data.get("next_page_token", "")
        if not next_page_token:
            break

    print(f"DEBUG total upcoming events: {len(events)}")
    for ev in events:
        print(f"  - {ev['topic']} | zoom_type={ev['zoom_type']} | is_simulive={ev['is_simulive']}")
    return events


def find_conflicts(new_start, new_duration_mins, existing_events, new_event_id, new_is_simulive):
    new_end = new_start + timedelta(minutes=new_duration_mins)
    conflicts = []
    for ev in existing_events:
        if str(ev["id"]) == str(new_event_id):
            continue
        if new_is_simulive and ev.get("is_simulive"):
            print(f"  SKIP simulive+simulive: {ev['topic']}")
            continue
        ev_end = ev["start"] + timedelta(minutes=ev["duration_mins"])
        overlaps = new_start < ev_end and new_end > ev["start"]
        print(f"  CHECK {ev['topic']} | simulive={ev['is_simulive']} | overlap={overlaps}")
        if overlaps:
            conflicts.append(ev)
    print(f"DEBUG conflicts found: {len(conflicts)}")
    return conflicts


def post_slack_alert(new_event, conflicts):
    conflict_lines = "\n".join(
        f"  • *{c['topic']}* ({c['type']}) — "
        f"{c['start'].strftime('%d %b %Y, %I:%M %p IST')} "
        f"for {c['duration_mins']} mins"
        for c in conflicts
    )
    message = {
        "text": "🚨 Zoom Scheduling Conflict Detected!",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🚨 Zoom Scheduling Conflict Detected!"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*New Event:* {new_event['topic']}\n"
                        f"*Type:* {new_event['event_kind']}\n"
                        f"*Host:* {new_event['host_email']}\n"
                        f"*Scheduled:* {new_event['start_time_fmt']} for {new_event['duration']} mins"
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Conflicts with:*\n{conflict_lines}",
                },
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "Please reschedule to avoid overlap."}
                ],
            },
        ],
    }
    httpx.post(SLACK_WEBHOOK_URL, json=message)


def verify_zoom_signature(body_bytes, timestamp, signature):
    message = f"v0:{timestamp}:{body_bytes.decode()}"
    expected = "v0=" + hmac.new(
        ZOOM_SECRET_TOKEN.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.route("/api/zoom_webhook", methods=["POST"])
def zoom_webhook():
    body_bytes = request.get_data()

    try:
        payload = json.loads(body_bytes)
    except Exception:
        return "Invalid JSON", 400

    if payload.get("event") == "endpoint.url_validation":
        plain_token = payload["payload"]["plainToken"]
        encrypted = hmac.new(
            ZOOM_SECRET_TOKEN.encode(), plain_token.encode(), hashlib.sha256
        ).hexdigest()
        return jsonify({"plainToken": plain_token, "encryptedToken": encrypted})

    timestamp = request.headers.get("x-zm-request-timestamp", "")
    signature = request.headers.get("x-zm-signature", "")
    if not verify_zoom_signature(body_bytes, timestamp, signature):
        return "Unauthorized", 401

    event_type = payload.get("event", "")
    if event_type not in ("webinar.created", "meeting.created"):
        return "Ignored", 200

    obj = payload.get("payload", {}).get("object", {})
    zoom_type = obj.get("type", 0)
    host_email = obj.get("host_email", "")
    topic = obj.get("topic", "Unknown")
    event_id = obj.get("id")
    start_time_raw = obj.get("start_time", "")
    duration = obj.get("duration", 0)

    print(f"DEBUG webhook → topic={topic} zoom_type={zoom_type} event_type={event_type}")

    try:
        token = get_zoom_access_token()

        # Always verify type from API for webinars
        if event_type == "webinar.created":
            zoom_type = get_webinar_actual_type(token, event_id)
            print(f"DEBUG verified zoom_type={zoom_type}")

        host_email = get_host_email(token, event_id, event_type)
        print(f"DEBUG host_email={host_email}")

    except Exception as e:
        print(f"ERROR fetching details: {e}")
        return f"Zoom API error: {e}", 500

    new_is_simulive = zoom_type in SIMULIVE_TYPES
    print(f"DEBUG new_is_simulive={new_is_simulive}")

    if new_is_simulive:
        event_kind = "Webinar (Simulive)"
    elif event_type == "webinar.created":
        event_kind = "Webinar (Live)"
    else:
        event_kind = "Meeting (Live)"

    if event_type == "webinar.created" and zoom_type not in LIVE_WEBINAR_TYPES | SIMULIVE_TYPES:
        return "Unrecognised webinar type — skipped", 200
    if event_type == "meeting.created" and zoom_type not in LIVE_MEETING_TYPES:
        return "Unrecognised meeting type — skipped", 200

    if not start_time_raw or not duration:
        return "No time data", 200

    new_start = datetime.fromisoformat(start_time_raw.replace("Z", "+00:00")) + IST_OFFSET

    try:
        existing = get_all_scheduled_events(token, host_email)
        conflicts = find_conflicts(new_start, duration, existing, event_id, new_is_simulive)
    except Exception as e:
        print(f"ERROR: {e}")
        return f"Zoom API error: {e}", 500

    if conflicts:
        post_slack_alert({
            "topic": topic,
            "event_kind": event_kind,
            "host_email": host_email,
            "start_time_fmt": new_start.strftime("%d %b %Y, %I:%M %p IST"),
            "duration": duration,
        }, conflicts)

    return "OK", 200


if __name__ == "__main__":
    app.run(debug=True)
