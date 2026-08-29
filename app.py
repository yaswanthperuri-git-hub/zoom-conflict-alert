def get_all_scheduled_events(token, host_email):
    headers = {"Authorization": f"Bearer {token}"}
    events = []

    user_id = host_email if host_email and host_email.strip() else "me"

    # Fetch meetings (paginated)
    next_page_token = ""
    while True:
        params = {"type": "scheduled", "page_size": 100}
        if next_page_token:
            params["next_page_token"] = next_page_token
        resp = httpx.get(
            f"https://api.zoom.us/v2/users/{user_id}/meetings",
            headers=headers,
            params=params,
        )
        if resp.status_code == 200:
            data = resp.json()
            for m in data.get("meetings", []):
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
            next_page_token = data.get("next_page_token", "")
            if not next_page_token:
                break
        else:
            break

    # Fetch webinars (paginated)
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
        if resp.status_code == 200:
            data = resp.json()
            for w in data.get("webinars", []):
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
            next_page_token = data.get("next_page_token", "")
            if not next_page_token:
                break
        else:
            break

    print(f"DEBUG total events fetched: {len(events)}")
    return events
