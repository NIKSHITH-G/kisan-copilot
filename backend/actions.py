"""Send advisories via Twilio WhatsApp/SMS.

Dry-run by design: with TWILIO_SID/TWILIO_TOKEN unset, every call returns the
exact payload that WOULD be sent (so the flow is testable end-to-end without
credentials). Paste creds into .env to go live — no code change.
"""

import os

import requests

TWILIO_WHATSAPP_SANDBOX = "whatsapp:+14155238886"  # Twilio's shared sandbox number


def _creds():
    return os.environ.get("TWILIO_SID"), os.environ.get("TWILIO_TOKEN")


def send_message(to: str, body: str, channel: str = "whatsapp") -> dict:
    """channel: 'whatsapp' or 'sms'. `to` is E.164, e.g. +919876543210."""
    to = to.strip()
    if not to.startswith("+"):
        return {"ok": False, "error": "recipient must be E.164, e.g. +91XXXXXXXXXX"}
    body = body.strip()[:1500]

    if channel == "whatsapp":
        from_ = os.environ.get("TWILIO_WHATSAPP_FROM") or TWILIO_WHATSAPP_SANDBOX
        if not from_.startswith("whatsapp:"):
            from_ = f"whatsapp:{from_}"
        to_addr = f"whatsapp:{to}"
    else:
        from_ = os.environ.get("TWILIO_SMS_FROM", "")
        to_addr = to

    sid, token = _creds()
    if not (sid and token):
        return {"ok": True, "dry_run": True, "channel": channel,
                "would_send": {"from": from_, "to": to_addr, "body": body},
                "note": "TWILIO_SID/TWILIO_TOKEN not set - message NOT sent"}

    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, token),
        data={"From": from_, "To": to_addr, "Body": body},
        timeout=30,
    )
    if r.status_code >= 300:
        return {"ok": False, "error": f"twilio http {r.status_code}: {r.text[:200]}"}
    j = r.json()
    return {"ok": True, "dry_run": False, "sid": j.get("sid"),
            "status": j.get("status"), "to": to_addr}
