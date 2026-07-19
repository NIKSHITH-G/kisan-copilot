"""MCP server exposing the advisory send action to CoCo (Cortex Code).

Register once:
  cortex mcp add kisan-actions -- <repo>/.venv/bin/python <repo>/backend/mcp_actions.py

Then inside cortex, after crop-advisory produces an answer, the agent can call
send_advisory to WhatsApp/SMS it to the farmer — data, reasoning AND action
all flow through CoCo. Dry-runs (no Twilio creds) return the exact payload
that would be sent.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp.server.fastmcp import FastMCP

import actions
import app as backend_app  # reuse its .env loader

backend_app.load_env()
mcp = FastMCP("kisan-actions")


@mcp.tool()
def send_advisory(to: str, message: str, channel: str = "whatsapp") -> dict:
    """Send a crop advisory to a farmer's phone via WhatsApp or SMS.

    Args:
        to: farmer's phone in E.164 format, e.g. +919876543210
        message: the advisory text (farmer's language; keep under 1500 chars)
        channel: 'whatsapp' (default) or 'sms'
    """
    return actions.send_message(to, message, channel)


if __name__ == "__main__":
    mcp.run()
