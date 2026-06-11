#!/usr/bin/env python3
import hashlib
import time
import requests
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HUBSPOT_TOKEN = os.environ["HUBSPOT_TOKEN"]
FB_PIXEL_ID = os.environ["FB_PIXEL_ID"]
FB_ACCESS_TOKEN = os.environ["FB_ACCESS_TOKEN"]
FB_API_VERSION = "v19.0"


def sha256_hash(value):
    if not value:
        return None
    return hashlib.sha256(str(value).strip().lower().encode()).hexdigest()


def normalize_phone(phone):
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        digits = "91" + digits  # Default India country code
    return digits


def build_fbc(raw_fbclid, createdate_str):
    """
    Meta CAPI requires fbc in format: fb.1.<creation_time_ms>.<fbclid>
    HubSpot stores only the raw fbclid token in hs_facebook_click_id.
    Use contact createdate as the click creation time approximation.
    """
    if not raw_fbclid:
        return None
    if raw_fbclid.startswith("fb."):
        return raw_fbclid  # Already in correct cookie format
    try:
        dt = datetime.fromisoformat(createdate_str.replace("Z", "+00:00"))
        creation_ms = int(dt.timestamp() * 1000)
    except Exception:
        creation_ms = int(time.time() * 1000)
    return f"fb.1.{creation_ms}.{raw_fbclid}"


def get_qualified_contacts():
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(minutes=20)).timestamp() * 1000)

    url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "filterGroups": [{
            "filters": [
                {"propertyName": "lastmodifieddate", "operator": "GTE", "value": str(cutoff_ms)},
                {"propertyName": "hs_lead_status", "operator": "HAS_PROPERTY"},
            ]
        }],
        "properties": ["email", "hs_facebook_click_id", "firstname", "lastname", "phone", "hs_lead_status", "hs_object_id", "createdate"],
        "limit": 100,
    }

    all_contacts = []
    after = None

    while True:
        if after:
            payload["after"] = after

        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

        for contact in data.get("results", []):
            status = (contact.get("properties", {}).get("hs_lead_status") or "").strip().lower()
            if status == "qualified":
                all_contacts.append(contact)

        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break

    return all_contacts


def send_capi_event(contact):
    props = contact.get("properties", {})
    phone = normalize_phone(props.get("phone"))
    fbc = build_fbc(props.get("hs_facebook_click_id"), props.get("createdate", ""))

    user_data = {
        "ct": [sha256_hash("bangalore")],
        "st": [sha256_hash("karnataka")],
        "country": [sha256_hash("in")],
        "ge": [sha256_hash("m")],
    }

    if props.get("email"):
        user_data["em"] = [sha256_hash(props["email"])]
    if phone:
        user_data["ph"] = [sha256_hash(phone)]
    if props.get("firstname"):
        user_data["fn"] = [sha256_hash(props["firstname"])]
    if props.get("lastname"):
        user_data["ln"] = [sha256_hash(props["lastname"])]
    if fbc:
        user_data["fbc"] = fbc  # sent as-is, not hashed

    event = {
        "event_name": "Lead",
        "event_time": int(time.time()),
        "action_source": "other",
        "event_id": str(props.get("hs_object_id", contact.get("id", ""))),
        "user_data": user_data,
    }

    response = requests.post(
        f"https://graph.facebook.com/{FB_API_VERSION}/{FB_PIXEL_ID}/events",
        json={"data": [event], "access_token": FB_ACCESS_TOKEN},
    )
    response.raise_for_status()
    return response.json()


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting HubSpot → Facebook CAPI sync")

    contacts = get_qualified_contacts()
    print(f"Found {len(contacts)} qualified contacts updated in the last 20 minutes")

    success, errors = 0, 0

    for contact in contacts:
        contact_id = contact.get("id")
        fbc_present = bool(contact.get("properties", {}).get("hs_facebook_click_id"))
        try:
            time.sleep(5)
            result = send_capi_event(contact)
            print(f"  Contact {contact_id}: Lead event sent (events_received={result.get('events_received', 0)}, fbc={'yes' if fbc_present else 'no'})")
            success += 1
        except Exception as e:
            print(f"  Contact {contact_id}: ERROR - {e}", file=sys.stderr)
            errors += 1

    print(f"Done. Success: {success}, Errors: {errors}")
    if errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
