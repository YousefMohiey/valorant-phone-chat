"""
Valorant Phone Chat Bridge
============================
A local bridge that lets you send chat messages to Valorant from your phone.
Runs on the same Windows PC as Valorant. Reads the Riot lockfile to get API
credentials, then proxies chat messages to Valorant's local HTTP API.

Vanguard-safe: uses Valorant's own internal API, not keyboard injection.
"""

import base64
import json
import logging
import os
import sys
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

# Constants

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
}

CACHE_TTL = 30

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

def get_team_chat_cid(preferred_type: str = "auto"):
    now = time.time()
    if _cache["cid"] and now < _cache["expires"] and (preferred_type == "auto" or _cache["chat_type"] == preferred_type):
        log.debug("Using cached CID: %s (%s)", _cache["cid"], _cache["chat_type"])
        return {
            "cid": _cache["cid"],
            "type": "groupchat" if _cache["chat_type"] != "dm" else "chat",
            "chat_type": _cache["chat_type"],
        }

    result = valorant_api("GET", "chat/v6/conversations")
    if result:
        log.info("Full conversations response: %s", json.dumps(result, indent=2))
        
        if "conversations" in result and result["conversations"]:
            if preferred_type in ("auto", "dm"):
                for conv in result["conversations"]:
                    cid = conv.get("cid", conv.get("id"))
                    ctype = conv.get("type", "")
                    log.info("Conversation: cid=%s type=%s", cid, ctype)
                    if ctype == "chat":
                        _cache["cid"] = cid
                        _cache["chat_type"] = "dm"
                        _cache["expires"] = now + CACHE_TTL
                        return {
                            "cid": cid,
                            "type": "chat",
                            "chat_type": "dm",
                        }

    log.warning("Trying specific endpoints for %s chat...", preferred_type)
    
    endpoints_to_try = []
    if preferred_type in ("auto", "team"):
        endpoints_to_try.append(("chat/v6/conversations/ares-coregame", "team"))
    if preferred_type in ("auto", "pregame"):
        endpoints_to_try.append(("chat/v6/conversations/ares-pregame", "pregame"))
    if preferred_type in ("auto", "party"):
        endpoints_to_try.append(("chat/v6/conversations/ares-parties", "party"))
    
    for endpoint, chat_type in endpoints_to_try:
        result = valorant_api("GET", endpoint)
        if result:
            log.info("%s response: %s", endpoint, json.dumps(result, indent=2))
            if "conversations" in result and result["conversations"]:
                for conv in result["conversations"]:
                    cid = conv.get("cid", conv.get("id"))
                    log.info("Found %s conversation: %s", chat_type, cid)
                    _cache["cid"] = cid
                    _cache["chat_type"] = chat_type
                    _cache["expires"] = now + CACHE_TTL
                    return {
                        "cid": cid,
                        "type": "groupchat",
                        "chat_type": chat_type,
                    }

    log.warning("No conversations found for type=%s", preferred_type)
    return None


def send_chat_message(message: str, preferred_type: str = "auto") -> dict:
    chat = get_team_chat_cid(preferred_type)
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


@app.route("/api/send", methods=["POST"])
def api_send():
    try:
        data = request.get_json(silent=True)
        if not data or "message" not in data:
            return jsonify({"success": False, "error": "Missing 'message' field"}), 400

        chat_type = data.get("chat_type", "auto")
        result = send_chat_message(data["message"].strip(), chat_type)
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
