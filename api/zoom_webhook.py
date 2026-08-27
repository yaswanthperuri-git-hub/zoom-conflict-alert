import json
import os
import hmac
import hashlib
import httpx
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler


ZOOM_SECRET_TOKEN = os.environ["ZOOM_SECRET_TOKEN"]
ZOOM_ACCOUNT_ID = os.environ["ZOOM_ACCOUNT_ID"]
ZOOM_CLIENT_ID = os.environ["ZOOM_CLIENT_ID"]
ZOOM_CLIENT_SECRET = os.environ["ZOOM_CLIENT_SECRET"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

SIMULIVE_TYPES = {9}
LIVE_WEBINAR_TYPES = {5, 6}
LIVE_MEETING_TYPES = {2, 3, 8}


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

    resp = httpx.get(
        f"https://api.zoom.us/v2/users/{host_email}/meetings",
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
                    "start": datetime.fromisoformat(m["start_time"].replace("Z", "+00:00")),
                    "duration_mins": m["duration"],
                })

    resp = httpx.get(
        f"https://api.zoom.us/v2/users/{host_email}/webinars",
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
                    "start": datetime.fromisoformat(w["start_time"].replace("Z", "+00:00")),
                    "duration_mins": w["duration"],
                })

    return events


def find_conflicts(new_start, new_duration_mins, existing_events, new_event_id):
    new_end = new_start + timedelta(minutes=new_duration_mins)
    conflicts = []
    for ev in existing_events:
        if str(ev["id"]) == str(new_event_id):
            continue
        ev_end = ev["start"] + timedelta(minutes=ev["duration_mins"])
        if new_start < ev_end and new_end > ev["start"]:
            conflicts.append(ev)
    return conflicts


def post_slack_alert(new_event, conflicts):
    conflict_lines = "\n".join(
        f"  • *{c['topic']}* ({c['type']}) — "
        f"{c['start'].strftime('%d %b %Y, %I:%M %p UTC')} "
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
                        f"*Type:* {new_event['event_kind']} (Live)\n"
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


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length)

        try:
            payload = json.loads(body_bytes)
        except Exception:
            self._respond(400, "Invalid JSON")
            return

        if payload.get("event") == "endpoint.url_validation":
            plain_token = payload["payload"]["plainToken"]
            encrypted = hmac.new(
                ZOOM_SECRET_TOKEN.encode(), plain_token.encode(), hashlib.sha256
            ).hexdigest()
            self._respond(200, json.dumps({
                "plainToken": plain_token,
                "encryptedToken": encrypted,
            }), content_type="application/json")
            return

        timestamp = self.headers.get("x-zm-request-timestamp", "")
        signature = self.headers.get("x-zm-signature", "")
        if not verify_zoom_signature(body_bytes, timestamp, signature):
            self._respond(401, "Unauthorized")
            return

        event_type = payload.get("event", "")
        if event_type not in ("webinar.created", "meeting.created"):
            self._respond(200, "Ignored")
            return

        obj = payload.get("payload", {}).get("object", {})
        zoom_type = obj.get("type", 0)
        host_email = obj.get("host_email", "")
        topic = obj.get("topic", "Unknown")
        event_id = obj.get("id")
        start_time_raw = obj.get("start_time", "")
        duration = obj.get("duration", 0)

        if zoom_type in SIMULIVE_TYPES:
            self._respond(200, "Simulive — skipped")
            return

        if event_type == "webinar.created" and zoom_type not in LIVE_WEBINAR_TYPES:
            self._respond(200, "Not a live webinar — skipped")
            return
        if event_type == "meeting.created" and zoom_type not in LIVE_MEETING_TYPES:
            self._respond(200, "Not a live meeting — skipped")
            return

        if not start_time_raw or not duration:
            self._respond(200, "No time data")
            return

        new_start = datetime.fromisoformat(start_time_raw.replace("Z", "+00:00"))
        event_kind = "Webinar" if event_type == "webinar.created" else "Meeting"

        try:
            token = get_zoom_access_token()
            existing = get_all_scheduled_events(token, host_email)
            conflicts = find_conflicts(new_start, duration, existing, event_id)
        except Exception as e:
            self._respond(500, f"Zoom API error: {e}")
            return

        if conflicts:
            post_slack_alert({
                "topic": topic,
                "event_kind": event_kind,
                "host_email": host_email,
                "start_time_fmt": new_start.strftime("%d %b %Y, %I:%M %p UTC"),
                "duration": duration,
            }, conflicts)

        self._respond(200, "OK")

    def _respond(self, status, body, content_type="text/plain"):
        encoded = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass
