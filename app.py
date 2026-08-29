import json
import os
import hmac
import hashlib
import httpx
from datetime import datetime, timedelta
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


def get_all_scheduled_events(token, host_email):
    headers = {"Authorization": f"Bearer {token}"}
    events = []

    user_id = host_email if host_email and host_email.strip() else "me"

    resp = httpx.get(
        f"https://api.zoom.us/v2/users/{user_id}/meetings",
        headers=headers,
        params={"type": "scheduled", "page_size": 100},
    )
    if resp.status_code == 200:
        for m in resp.json().get("meetings", []):
            if m.get("start_time") and m.get("duration"):
                events.append({
                    "id": m["id"],
                    "topic": m["topic"],
                    "type": "Meeting",
                    "zoom_type": m.get("type", 0),
                    "is_simulive": m.get("type", 0) in SIMULIVE_TYPES,
                    "start": datetime.fromisoformat(m["start_time"].replace("Z", "+00:00")) + IST_OFFSET,
                    "duration_mins": m["duration"],
                })

    resp = httpx.get(
        f"https://api.zoom.us/v2/users/{user_id}/webinars",
        headers=headers,
        params={"page_size": 100},
    )
    if resp.status_code == 200:
        for w in resp.json().get("webinars", []):
            if w.get("start_time") and w.get("duration"):
                events.append({
                    "id": w["id"],
                    "topic": w["topic"],
                    "type": "Webinar",
                    "zoom_type": w.get("type", 0),
                    "is_simulive": w.get("type", 0) in SIMULIVE_TYPES,
                    "start": datetime.fromisoformat(w["start_time"].replace("Z", "+00:00")) + IST_OFFSET,
                    "duration_mins": w["duration"],
                })

    return events


def find_conflicts(new_start, new_duration_mins, existing_events, new_event_id, new_is_simulive):
    new_end = new_start + timedelta(minutes=new_duration_mins)
    conflicts = []
    for ev in existing_events:
        if str(ev["id"]) == str(new_event_id):
            continue

        # Only ignore if BOTH new and existing are Simulive
        # Zoom allows up to 3 simultaneous Simulive webinars
        if new_is_simulive and ev.get("is_simulive"):
            continue

        ev_end = ev["start"] + timedelta(minutes=ev["duration_mins"])
        if new_start < ev_end and new_end > ev["start"]:
            conflicts.append(ev)
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

    # Determine if new event is Simulive
    new_is_simulive = zoom_type in SIMULIVE_TYPES

    # Determine event kind label
    if new_is_simulive:
        event_kind = "Webinar (Simulive)"
    elif event_type == "webinar.created":
        event_kind = "Webinar (Live)"
    else:
        event_kind = "Meeting (Live)"

    # Skip unrecognised types
    if event_type == "webinar.created" and zoom_type not in LIVE_WEBINAR_TYPES | SIMULIVE_TYPES:
        return "Unrecognised webinar type — skipped", 200
    if event_type == "meeting.created" and zoom_type not in LIVE_MEETING_TYPES:
        return "Unrecognised meeting type — skipped", 200

    if not start_time_raw or not duration:
        return "No time data", 200

    # Convert to IST
    new_start = datetime.fromisoformat(start_time_raw.replace("Z", "+00:00")) + IST_OFFSET

    try:
        token = get_zoom_access_token()
        existing = get_all_scheduled_events(token, host_email)
        conflicts = find_conflicts(new_start, duration, existing, event_id, new_is_simulive)
    except Exception as e:
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
