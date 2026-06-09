"""
Valorant Phone Chat Bridge
============================
A local bridge that lets you send chat messages to Valorant from your phone.
Runs on the same Windows PC as Valorant. Reads the Riot lockfile to get API
credentials, then proxies chat messages to Valorant's local HTTP API.

Vanguard-safe: uses Valorant's own internal API, not keyboard injection.

How it discovers team/party chat CIDs:
    Uses ONLY the local API endpoints:
      - GET /chat/v6/conversations/ares-coregame   (in-match team/all chat)
      - GET /chat/v6/conversations/ares-parties    (party chat)
      - GET /chat/v6/conversations                 (all conversations, for DMs)

    No external GLZ calls — the glz-*.a.pvp.net hostnames are not
    publicly resolvable via standard DNS, so any third-party HTTP
    client (like Python's requests) cannot reach them. The local
    API exposes the same conversation data without DNS headaches.
"""

import base64
import json
import logging
import os
import sys
import threading
import time
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

# ── Lockfile discovery ────────────────────────────────────────────────────


def find_valorant_lockfile():
    import psutil

    lockfile_candidates = []

    for proc in psutil.process_iter(['pid', 'name', 'exe']):
        try:
            name = proc.info['name'] or ''
            if 'VALORANT' in name.upper() and 'Shipping' in name:
                valorant_pid = proc.info['pid']
                log.info("Found Valorant process: PID=%s, Name=%s", valorant_pid, name)

                if proc.info['exe']:
                    game_dir = os.path.dirname(proc.info['exe'])
                    potential_paths = [
                        os.path.join(game_dir, "ShooterGame", "Saved", "Config", "lockfile"),
                        os.path.join(game_dir, "lockfile"),
                        os.path.join(os.path.dirname(game_dir), "lockfile"),
                    ]
                    for path in potential_paths:
                        if os.path.exists(path):
                            lockfile_candidates.insert(0, path)
                            log.info("Found potential game lockfile: %s", path)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    riot_folder = os.path.expandvars(r"%LocalAppData%\Riot Games")
    if os.path.exists(riot_folder):
        for root, dirs, files in os.walk(riot_folder):
            if "lockfile" in files:
                lf_path = os.path.join(root, "lockfile")
                if lf_path not in lockfile_candidates:
                    lockfile_candidates.append(lf_path)
                    log.info("Found lockfile in search: %s", lf_path)

    for path in lockfile_candidates:
        try:
            with open(path, "r") as f:
                content = f.read().strip()
            parts = content.split(":")
            if len(parts) >= 4:
                name = parts[0]
                if "Riot Client" in name and len(lockfile_candidates) > 1:
                    log.info("Skipping Riot Client lockfile: %s", path)
                    continue

                log.info("Using lockfile: %s (name=%s, port=%s)", path, name, parts[2])
                return {
                    "path": path,
                    "name": name,
                    "pid": parts[1],
                    "port": parts[2],
                    "password": parts[3],
                    "protocol": parts[4] if len(parts) > 4 else "https",
                }
        except Exception as e:
            log.warning("Error reading %s: %s", path, e)

    for path in lockfile_candidates:
        try:
            with open(path, "r") as f:
                content = f.read().strip()
            parts = content.split(":")
            if len(parts) >= 4:
                log.info("Fallback to lockfile: %s", path)
                return {
                    "path": path,
                    "name": parts[0],
                    "pid": parts[1],
                    "port": parts[2],
                    "password": parts[3],
                    "protocol": parts[4] if len(parts) > 4 else "https",
                }
        except Exception as e:
            log.warning("Error reading %s: %s", path, e)

    log.warning("No lockfile found")
    return None

# Cache for conversation CIDs (avoid spamming local API)
_cache = {
    "cid": None,
    "chat_type": None,
    "expires": 0,
    "session": None,
    "session_expires": 0,
    "all_conversations": None,
    "all_conversations_expires": 0,
}

CACHE_TTL = 60  # shorter TTL so we re-discover when user enters/leaves matches

_last_send_time = 0
SEND_COOLDOWN = 30

# ── Lockfile ───────────────────────────────────────────────────────────────


def read_lockfile():
    lockfile = find_valorant_lockfile()
    if not lockfile:
        log.warning("No lockfile found")
        return None

    log.info("Using lockfile: %s (port=%s)", lockfile["path"], lockfile["port"])
    return lockfile


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
        log.info("API %s %s -> %d", method, endpoint, response.status_code)
        if response.status_code == 200:
            return response.json()
        try:
            log.warning("API %s %s returned %d: %s", method, endpoint, response.status_code, response.text[:500])
        except Exception:
            log.warning("API %s %s returned %d (no body)", method, endpoint, response.status_code)
        return None
    except requests.RequestException as e:
        log.error("API request failed: %s", e)
        return None
    except Exception as e:
        log.error("Unexpected API error: %s", e)
        return None


# ── Chat helpers ───────────────────────────────────────────────────────────


def get_puuid() -> str | None:
    """Get the player's PUUID from the local session API."""
    now = time.time()
    if _cache["session"] and now < _cache["session_expires"]:
        puuid = _cache["session"].get("puuid")
        if puuid:
            return puuid

    session = valorant_api("GET", "chat/v1/session")
    if session:
        _cache["session"] = session
        _cache["session_expires"] = now + CACHE_TTL
        puuid = session.get("puuid")
        if puuid:
            log.info("Player PUUID: %s", puuid)
            return puuid
    return None


def classify_conversation(cid: str) -> str:
    """Classify a conversation CID into a chat type label."""
    if not cid or "@" not in cid:
        return "unknown"
    domain = cid.split("@", 1)[1].lower()
    if "ares-coregame" in domain:
        return "coregame"  # in-match team/all chat
    if "ares-pregame" in domain:
        return "pregame"   # agent select chat
    if "ares-parties" in domain:
        return "party"     # party/lobby chat
    if "pvp.net" in domain:
        return "dm"        # direct message
    return "unknown"


def conversation_label(cid: str, ctype: str) -> str:
    """Generate a human-readable label for a conversation."""
    if ctype == "coregame":
        # Team chat format: {match-id}-{team}@ares-coregame.{shard}.pvp.net
        team = cid.split("@")[0].rsplit("-", 1)[-1]
        if team == "blue":
            return "Team (Blue)"
        elif team == "red":
            return "Enemy (Red)"
        elif team == "all":
            return "All Chat"
        return f"Match ({team})"
    elif ctype == "pregame":
        return "Pregame (Agent Select)"
    elif ctype == "party":
        return "Party"
    elif ctype == "dm":
        return "DM"
    return "Chat"


def get_all_conversations() -> list[dict]:
    """Fetch all conversations from the local API, categorized by type."""
    now = time.time()
    if _cache["all_conversations"] and now < _cache["all_conversations_expires"]:
        return _cache["all_conversations"]

    # Query all three ares-* endpoints in one go for efficiency
    all_convs = []

    for endpoint, label in [
        ("chat/v6/conversations/ares-coregame", "coregame"),
        ("chat/v6/conversations/ares-pregame", "pregame"),
        ("chat/v6/conversations/ares-parties", "party"),
        ("chat/v6/conversations", "dm"),
    ]:
        result = valorant_api("GET", endpoint)
        if result and "conversations" in result:
            for conv in result["conversations"]:
                cid = conv.get("cid", "")
                ctype = classify_conversation(cid)
                # If classify_conversation couldn't determine type from domain,
                # use the endpoint label
                if ctype == "unknown":
                    ctype = label
                all_convs.append({
                    "cid": cid,
                    "type": ctype,
                    "chat_type": conv.get("type", "groupchat"),
                    "unread": conv.get("unread_count", 0),
                    "muted": conv.get("muted", False),
                })
                log.info("Found conversation: cid=%s type=%s", cid, ctype)

    # Deduplicate by CID (some endpoints may overlap)
    seen = set()
    unique = []
    for conv in all_convs:
        if conv["cid"] not in seen:
            seen.add(conv["cid"])
            unique.append(conv)

    _cache["all_conversations"] = unique
    _cache["all_conversations_expires"] = now + CACHE_TTL
    return unique


def get_team_chat_cid(preferred_type: str = "auto") -> dict | None:
    """Discover the best chat CID for the current game state.

    Priority:
        1. Team chat (ares-coregame) - in a match
        2. Pregame chat (ares-pregame) - in agent select
        3. Party chat (ares-parties) - in a party
        4. DM - last resort fallback

    Returns dict with {cid, type, chat_type} or None.
    """
    now = time.time()
    if (
        _cache["cid"]
        and now < _cache["expires"]
        and (preferred_type == "auto" or _cache["chat_type"] == preferred_type)
    ):
        log.debug("Using cached CID: %s (%s)", _cache["cid"], _cache["chat_type"])
        return {
            "cid": _cache["cid"],
            "type": "groupchat",
            "chat_type": _cache["chat_type"],
        }

    # Ensure PUUID is available (needed for the /chat/v6/session call)
    puuid = get_puuid()
    if not puuid:
        log.warning("Cannot get PUUID")
        return None

    conversations = get_all_conversations()
    if not conversations:
        log.warning("No conversations found")
        return None

    # Build priority list based on preference
    if preferred_type == "team":
        priority = ["coregame", "pregame", "party", "dm"]
    elif preferred_type == "party":
        priority = ["party", "coregame", "pregame", "dm"]
    elif preferred_type == "dm":
        priority = ["dm", "coregame", "pregame", "party"]
    else:  # auto
        priority = ["coregame", "pregame", "party", "dm"]

    for ctype in priority:
        for conv in conversations:
            if conv["type"] == ctype:
                cid = conv["cid"]
                msg_type = conv["chat_type"]  # "groupchat" or "chat"
                # For coregame: prefer the "blue" team conversation (your team)
                # Skip "red" (enemy) and "all" unless explicitly asked
                if ctype == "coregame" and preferred_type == "auto":
                    team = cid.split("@")[0].rsplit("-", 1)[-1]
                    if team not in ("blue", "all"):
                        # Skip red (enemy team) in auto mode
                        continue
                log.info("Selected chat: cid=%s type=%s (preferred=%s)", cid, ctype, preferred_type)
                _cache["cid"] = cid
                _cache["chat_type"] = ctype
                _cache["expires"] = now + CACHE_TTL
                return {
                    "cid": cid,
                    "type": msg_type,
                    "chat_type": ctype,
                }

    # Final fallback: return first available
    if conversations:
        conv = conversations[0]
        log.info("Fallback to first conversation: cid=%s type=%s", conv["cid"], conv["type"])
        _cache["cid"] = conv["cid"]
        _cache["chat_type"] = conv["type"]
        _cache["expires"] = now + CACHE_TTL
        return {
            "cid": conv["cid"],
            "type": conv["chat_type"],
            "chat_type": conv["type"],
        }

    return None


def send_chat_message(message: str, preferred_type: str = "auto") -> dict:
    chat = get_team_chat_cid(preferred_type)
    if not chat:
        return {
            "success": False,
            "error": "No active chat conversation found. Are you in a game or party?",
        }

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


def get_status():
    now = time.time()

    if _cache["session"] and now < _cache["session_expires"]:
        session = _cache["session"]
    else:
        session = valorant_api("GET", "chat/v1/session")
        if session:
            _cache["session"] = session
            _cache["session_expires"] = now + CACHE_TTL

    if not session:
        lockfile = read_lockfile()
        if not lockfile:
            return {"valorant_running": False, "chat_ready": False}
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


@app.route("/api/conversations")
def api_conversations():
    """Return all available conversations with labels."""
    conversations = []
    try:
        all_convs = get_all_conversations()
        puuid = get_puuid()

        for conv in all_convs:
            cid = conv["cid"]
            ctype = conv["type"]
            label = conversation_label(cid, ctype)

            # For DMs, try to get the other person's name
            if ctype == "dm":
                name = "Unknown"
                try:
                    participants = valorant_api("GET", f"chat/v5/participants?cid={cid}")
                    if participants and "participants" in participants:
                        for p in participants["participants"]:
                            if p.get("puuid") != puuid:
                                name = f"{p.get('game_name', '?')}#{p.get('game_tag', '?')}"
                                break
                except Exception:
                    pass
            else:
                name = label

            conversations.append({
                "cid": cid,
                "name": name,
                "label": label,
                "type": ctype,
                "chat_type": conv["chat_type"],
                "unread": conv.get("unread", 0),
            })
    except Exception as e:
        log.error("Conversations endpoint error: %s\n%s", e, traceback.format_exc())

    return jsonify({"conversations": conversations})


@app.route("/api/debug")
def api_debug():
    endpoints_found = []

    for ep in ["help", "chat/v6/conversations/ares-coregame", "chat/v6/conversations/ares-pregame", "chat/v6/conversations/ares-parties", "chat/v6/conversations"]:
        result = valorant_api("GET", ep)
        if result:
            convs = result.get("conversations", [])
            endpoints_found.append({
                "endpoint": ep,
                "status": "200",
                "conversation_count": len(convs),
                "cids": [c.get("cid") for c in convs],
            })
        else:
            endpoints_found.append({"endpoint": ep, "status": "failed"})

    return jsonify({"endpoints": endpoints_found})


@app.route("/api/send", methods=["POST"])
def api_send():
    global _last_send_time

    now = time.time()
    if now - _last_send_time < SEND_COOLDOWN:
        wait = int(SEND_COOLDOWN - (now - _last_send_time))
        return jsonify({"success": False, "error": f"Rate limited. Wait {wait}s"}), 429

    try:
        data = request.get_json(silent=True)
        if not data or "message" not in data:
            return jsonify({"success": False, "error": "Missing 'message' field"}), 400

        direct_cid = data.get("cid")
        if direct_cid:
            result = send_to_cid(direct_cid, data["message"].strip())
        else:
            chat_type = data.get("chat_type", "auto")
            result = send_chat_message(data["message"].strip(), chat_type)

        _last_send_time = time.time()
        status_code = 200 if result.get("success") else 400
        log.info("Send result: %s", result)
        return jsonify(result), status_code
    except Exception as e:
        log.error("Send endpoint crashed: %s\n%s", e, traceback.format_exc())
        return jsonify({"success": False, "error": str(e)}), 500


def send_to_cid(cid, message):
    result = valorant_api(
        "POST",
        "chat/v6/messages",
        data={
            "cid": cid,
            "message": message,
            "type": "chat",
        },
    )
    if result:
        return {"success": True, "message": message, "chat_type": "dm"}
    return {"success": False, "error": "Failed to send message"}


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

    for ep in ["help", "swagger/v3/openapi.json"]:
        result = valorant_api("GET", ep)
        if result:
            log.info("Endpoint %s: %s", ep, json.dumps(result)[:1000])

    print()
    print("  ========================================================")
    print("  |        VALORANT PHONE CHAT BRIDGE v1.1              |")
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
