"""
Valorant Phone Chat Bridge
============================
A local bridge that lets you send chat messages to Valorant from your phone.
Runs on the same Windows PC as Valorant. Reads the Riot lockfile to get API
credentials, then proxies chat messages to Valorant's local HTTP API.

Vanguard-safe: uses Valorant's own internal API, not keyboard injection.
"""

import base64
import logging
import os
import sys
import traceback
from datetime import datetime

import requests
import urllib3
from flask import Flask, jsonify, request, render_template

# Logging

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.log")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("bridge")

log.info("Logger initialized. Log file: %s", LOG_FILE)

# ── Flask setup ────────────────────────────────────────────────────────────

app = Flask(__name__)

# Constants

LOCKFILE_PATH = os.path.expandvars(
    r"%LocalAppData%\Riot Games\Riot Client\Config\lockfile"
)

log.info("Lockfile path: %s", LOCKFILE_PATH)

# ── Lockfile ───────────────────────────────────────────────────────────────

def read_lockfile():
    try:
        with open(LOCKFILE_PATH, "r") as f:
            parts = f.read().strip().split(":")
        if len(parts) < 4:
            log.warning("Lockfile has unexpected format: %s", parts)
            return None
        lockfile = {
            "name": parts[0],
            "pid": parts[1],
            "port": parts[2],
            "password": parts[3],
            "protocol": parts[4] if len(parts) > 4 else "https",
        }
        log.debug("Lockfile loaded: port=%s, protocol=%s", lockfile["port"], lockfile["protocol"])
        return lockfile
    except FileNotFoundError:
        log.warning("Lockfile not found at: %s", LOCKFILE_PATH)
        return None
    except Exception as e:
        log.error("Error reading lockfile: %s", e)
        return None


# ── Valorant Local API client ──────────────────────────────────────────────

# The local API uses a self-signed cert. Disable SSL warnings.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def valorant_api(method: str, endpoint: str, data: dict | None = None):
    lockfile = read_lockfile()
    if not lockfile:
        log.warning("Cannot call API: no lockfile")
        return None

    base_url = f"https://127.0.0.1:{lockfile['port']}"
    auth_str = f"riot:{lockfile['password']}"
    auth_b64 = base64.b64encode(auth_str.encode()).decode()

    headers = {
        "Authorization": f"Basic {auth_b64}",
        "Content-Type": "application/json",
    }

    try:
        log.debug("API %s %s", method, endpoint)
        response = requests.request(
            method=method,
            url=f"{base_url}/{endpoint}",
            headers=headers,
            json=data,
            verify=False,
            timeout=5,
        )
        log.debug("API response: %d", response.status_code)
        if response.status_code == 200:
            return response.json()
        log.warning("API returned %d for %s", response.status_code, endpoint)
        return None
    except requests.RequestException as e:
        log.error("API request failed: %s", e)
        return None
    except Exception as e:
        log.error("Unexpected API error: %s", e)
        return None


# ── Chat helpers ───────────────────────────────────────────────────────────

def get_team_chat_cid() -> dict | None:
    """
    Discover the team chat conversation ID from the current game session.
    Returns the 'cid' for the team chat channel.
    """
    # Try game chat first (in-match)
    result = valorant_api("GET", "chat/v6/conversations/ares-coregame")
    if result and "conversations" in result:
        for conv in result["conversations"]:
            # team chat has type "groupchat"
            return {
                "cid": conv.get("cid", conv.get("id")),
                "type": "groupchat",
                "chat_type": "team",
            }

    # Try pre-game chat (agent select)
    result = valorant_api("GET", "chat/v6/conversations/ares-pregame")
    if result and "conversations" in result:
        for conv in result["conversations"]:
            return {
                "cid": conv.get("cid", conv.get("id")),
                "type": "groupchat",
                "chat_type": "pregame",
            }

    # Try party chat
    result = valorant_api("GET", "chat/v6/conversations/ares-parties")
    if result and "conversations" in result:
        for conv in result["conversations"]:
            return {
                "cid": conv.get("cid", conv.get("id")),
                "type": "groupchat",
                "chat_type": "party",
            }

    return None


def send_chat_message(message: str) -> dict:
    """Send a chat message to the current team/match chat."""
    chat = get_team_chat_cid()
    if not chat:
        return {"success": False, "error": "No active chat conversation found. Are you in a game?"}

    result = valorant_api(
        "POST",
        "chat/v6/messages",
        data={
            "cid": chat["cid"],
            "message": message,
            "type": chat["type"],
        },
    )

    if result:
        return {"success": True, "message": message, "chat_type": chat["chat_type"]}
    else:
        return {"success": False, "error": "Failed to send message. Is Valorant running?"}


# ── Status ─────────────────────────────────────────────────────────────────

def get_status() -> dict:
    """Check if Valorant is running and chat is available."""
    lockfile = read_lockfile()
    if not lockfile:
        return {"valorant_running": False, "chat_ready": False}

    # Verify the local API is reachable
    session = valorant_api("GET", "chat/v1/session")
    if not session:
        return {"valorant_running": True, "chat_ready": False, "error": "API unreachable"}

    chat = get_team_chat_cid()

    return {
        "valorant_running": True,
        "chat_ready": chat is not None,
        "chat_type": chat["chat_type"] if chat else None,
        "player_name": f"{session.get('game_name', '?')}#{session.get('game_tag', '?')}",
    }


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the mobile web UI."""
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    try:
        return jsonify(get_status())
    except Exception as e:
        log.error("Status endpoint crashed: %s\n%s", e, traceback.format_exc())
        return jsonify({"valorant_running": False, "chat_ready": False, "error": str(e)}), 500


@app.route("/api/send", methods=["POST"])
def api_send():
    try:
        data = request.get_json(silent=True)
        if not data or "message" not in data:
            return jsonify({"success": False, "error": "Missing 'message' field"}), 400

        result = send_chat_message(data["message"].strip())
        status_code = 200 if result.get("success") else 400
        log.info("Send result: %s", result)
        return jsonify(result), status_code
    except Exception as e:
        log.error("Send endpoint crashed: %s\n%s", e, traceback.format_exc())
        return jsonify({"success": False, "error": str(e)}), 500


# ── Main ───────────────────────────────────────────────────────────────────

def get_local_ip() -> str:
    """
    Get the actual LAN IP address (not 127.0.0.1).
    Connects a dummy UDP socket to a public address to
    discover the real network interface IP.
    """
    import socket

    try:
        # Connect to a public DNS server to discover the local IP.
        # No data is actually sent — UDP is connectionless.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        # Fallback to hostname resolution
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


if __name__ == "__main__":
    local_ip = get_local_ip()

    print()
    print("  ========================================================")
    print("  |        VALORANT PHONE CHAT BRIDGE v1.0              |")
    print("  |======================================================|")
    print("  |                                                      |")
    print("  |   On your phone, open:                               |")
    print(f"  |   http://{local_ip}:8080              |")
    print("  |                                                      |")
    print("  |   Phone & PC must be on the same WiFi network.       |")
    print("  |   Valorant must be running and in a match.           |")
    print("  |                                                      |")
    print("  |   Press Ctrl+C to stop.                              |")
    print("  ========================================================")
    print()

    app.run(host="0.0.0.0", port=8080, debug=False)
