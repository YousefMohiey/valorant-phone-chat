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

# Live game state, updated by the background WebSocket listener
_game_state = {
    "match_id": None,
    "party_id": None,
    "session_state": None,
    "shard": "eu1",
    "last_update": 0,
}

CACHE_TTL = 60
GAME_STATE_TTL = 300

_last_send_time = 0
SEND_COOLDOWN = 0

_ws_thread_started = False
_ws_lock = threading.Lock()

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


# ── WebSocket listener for live game state ─────────────────────────────────


def _ws_send(sock, text):
    data = text.encode()
    frame = bytearray()
    frame.append(0x81)
    length = len(data)
    if length < 126:
        frame.append(length | 0x80)
    elif length < 65536:
        frame.append(126 | 0x80)
        frame.extend(length.to_bytes(2, "big"))
    else:
        frame.append(127 | 0x80)
        frame.extend(length.to_bytes(8, "big"))
    mask = os.urandom(4)
    frame.extend(mask)
    masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(data))
    frame.extend(masked)
    sock.send(bytes(frame))


def _ws_recv(sock):
    try:
        header = sock.recv(2)
        if len(header) < 2:
            return None
        opcode = header[0] & 0x0F
        if opcode == 0x8:
            return None
        length = header[1] & 0x7F
        if length == 126:
            ext = sock.recv(2)
            if len(ext) < 2:
                return None
            length = int.from_bytes(ext, "big")
        elif length == 127:
            ext = sock.recv(8)
            if len(ext) < 8:
                return None
            length = int.from_bytes(ext, "big")
        payload = b""
        while len(payload) < length:
            chunk = sock.recv(length - len(payload))
            if not chunk:
                return None
            payload += chunk
        return payload
    except Exception:
        return None


def _parse_ws_message(data):
    try:
        text = data.decode("utf-8")
        return json.loads(text)
    except Exception:
        return None


def _extract_game_state_from_event(msg):
    """Extract match_id, party_id, and session_state from a WebSocket event."""
    if not isinstance(msg, list) or len(msg) < 3:
        return
    event = msg[2]
    if not isinstance(event, dict):
        return
    uri = event.get("uri", "")
    data = event.get("data", {})

    verbose = os.environ.get("BRIDGE_WS_VERBOSE", "").lower() in ("1", "true", "yes")

    if verbose and "riot-messaging-service" in uri:
        log.info("WS-EVENT: %s", uri)

    if "ares-pregame/pregame/v1/matches/" in uri:
        parts = uri.split("/")
        match_id = parts[-1] if parts else None
        if match_id:
            with _ws_lock:
                _game_state["match_id"] = match_id
                _game_state["session_state"] = "PREGAME"
                _game_state["last_update"] = time.time()
            log.info("WS: pregame match_id=%s", match_id)
    elif "ares-core-game/core-game/v1/matches/" in uri:
        parts = uri.split("/")
        match_id = parts[-1] if parts else None
        if match_id:
            with _ws_lock:
                _game_state["match_id"] = match_id
                _game_state["session_state"] = "INGAME"
                _game_state["last_update"] = time.time()
            log.info("WS: ingame match_id=%s", match_id)
    elif "ares-parties/parties/v1/parties/" in uri:
        parts = uri.split("/")
        party_id = parts[-1] if parts else None
        if party_id:
            with _ws_lock:
                _game_state["party_id"] = party_id
                _game_state["last_update"] = time.time()
            log.info("WS: party_id=%s", party_id)
    elif "ares-session/v1/sessions/" in uri:
        try:
            payload = data.get("payload", "{}")
            if isinstance(payload, str):
                session_data = json.loads(payload)
                state = session_data.get("loopState")
                if state:
                    with _ws_lock:
                        _game_state["session_state"] = state
                        _game_state["last_update"] = time.time()
            if verbose and isinstance(payload, str):
                log.debug("WS-SESSION: %s", payload[:200])
        except Exception:
            pass


def _ws_listener_loop():
    """Background thread that connects to the local WebSocket and tracks game state."""
    import ssl
    import socket
    from base64 import b64encode

    while True:
        try:
            lockfile = read_lockfile()
            if not lockfile:
                time.sleep(5)
                continue

            auth = b64encode(f"riot:{lockfile['password']}".encode()).decode()

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ssl_sock = ctx.wrap_socket(sock, server_hostname="127.0.0.1")
            ssl_sock.connect(("127.0.0.1", int(lockfile["port"])))

            key = b64encode(os.urandom(16)).decode()
            handshake = (
                f"GET / HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{lockfile['port']}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"Authorization: Basic {auth}\r\n"
                f"\r\n"
            )
            ssl_sock.send(handshake.encode())
            response = ssl_sock.recv(4096)
            if b"101" not in response:
                ssl_sock.close()
                time.sleep(5)
                continue

            _ws_send(ssl_sock, json.dumps([5, "OnJsonApiEvent"]))
            _ws_send(ssl_sock, json.dumps([5, "OnJsonApiEvent_chat_v4_presences"]))
            _ws_send(ssl_sock, json.dumps([5, "OnJsonApiEvent_chat_v6_messages"]))
            log.info("WebSocket listener connected and subscribed to chat events")

            while True:
                data = _ws_recv(ssl_sock)
                if data is None:
                    break
                msg = _parse_ws_message(data)
                if msg:
                    _extract_game_state_from_event(msg)

            ssl_sock.close()
        except Exception as e:
            log.debug("WebSocket listener error: %s", e)

        time.sleep(3)


def start_ws_listener():
    """Start the background WebSocket listener thread (once)."""
    global _ws_thread_started
    with _ws_lock:
        if _ws_thread_started:
            return
        _ws_thread_started = True
    t = threading.Thread(target=_ws_listener_loop, daemon=True, name="ws-listener")
    t.start()
    log.info("WebSocket listener thread started")


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


def get_user_presence() -> dict | None:
    """
    Get the current user's presence data from the local API.

    The /chat/v4/presences endpoint returns all online friends AND the current
    user's own presence. The `private` field is base64-encoded JSON containing
    party ID, match ID, session state, etc.
    """
    puuid = get_puuid()
    if not puuid:
        return None

    result = valorant_api("GET", "chat/v4/presences")
    if not result or "presences" not in result:
        return None

    for presence in result["presences"]:
        if presence.get("puuid") == puuid:
            private_b64 = presence.get("private")
            if private_b64:
                try:
                    private_json = base64.b64decode(private_b64).decode("utf-8")
                    private_data = json.loads(private_json)
                    presence["_private_decoded"] = private_data
                except Exception as e:
                    log.warning("Failed to decode private presence: %s", e)
            return presence

    return None


def get_shard_from_pid() -> str:
    """Determine the player's shard (e.g. 'eu1', 'eu2') from their PID."""
    session = _cache.get("session")
    if session:
        pid = session.get("pid", "")
        if "@" in pid:
            domain = pid.split("@", 1)[1]
            if "." in domain:
                shard = domain.split(".")[0]
                if shard:
                    return shard
    return "eu1"


def get_match_and_party_ids() -> dict:
    """
    Extract match ID and party ID from live game state.

    Prefers the WebSocket-tracked state (most up-to-date), falls back
    to the presence API for the party ID if not yet seen via WS.

    Returns dict with keys: match_id, party_id, session_state, shard
    """
    shard = get_shard_from_pid()
    result = {
        "match_id": None,
        "party_id": None,
        "session_state": None,
        "shard": shard,
    }

    with _ws_lock:
        ws_state_age = time.time() - _game_state["last_update"] if _game_state["last_update"] else float("inf")
        if ws_state_age < GAME_STATE_TTL:
            result["match_id"] = _game_state["match_id"]
            result["party_id"] = _game_state["party_id"]
            result["session_state"] = _game_state["session_state"]

    if not result["party_id"]:
        presence = get_user_presence()
        if presence:
            private = presence.get("_private_decoded", {})
            if not result["session_state"]:
                result["session_state"] = private.get("sessionLoopState", "MENUS")
            party_presence = private.get("partyPresenceData", {})
            if party_presence:
                result["party_id"] = party_presence.get("partyId")

    if not result["session_state"]:
        result["session_state"] = "MENUS"

    with _ws_lock:
        _game_state["shard"] = shard

    log.info(
        "Game state: match_id=%s party_id=%s state=%s shard=%s",
        result["match_id"],
        result["party_id"],
        result["session_state"],
        result["shard"],
    )

    return result


def construct_team_chat_cid(match_id: str, shard: str = "eu1") -> str:
    """Construct a team chat CID from a match ID.

    Format: {match-id}-blue@ares-coregame.{shard}.pvp.net
    """
    return f"{match_id}-blue@ares-coregame.{shard}.pvp.net"


def construct_all_chat_cid(match_id: str, shard: str = "eu1") -> str:
    """Construct an all-chat CID from a match ID.

    Format: {match-id}-all@ares-coregame.{shard}.pvp.net
    """
    return f"{match_id}-all@ares-coregame.{shard}.pvp.net"


def construct_pregame_chat_cid(match_id: str, shard: str = "eu1") -> str:
    """Construct a pregame (agent select) chat CID from a match ID.

    Format: {match-id}@ares-pregame.{shard}.pvp.net
    """
    return f"{match_id}@ares-pregame.{shard}.pvp.net"


def construct_party_chat_cid(party_id: str, shard: str = "eu1") -> str:
    """Construct a party chat CID from a party ID.

    Format: {party-id}@ares-parties.{shard}.pvp.net
    """
    return f"{party_id}@ares-parties.{shard}.pvp.net"


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

    Strategy:
        1. Get match ID and party ID from the user's presence data
        2. Construct CIDs directly (bypasses lazy-creation issue)
        3. Fall back to local API conversation list for DMs

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

    puuid = get_puuid()
    if not puuid:
        log.warning("Cannot get PUUID")
        return None

    ids = get_match_and_party_ids()
    session_state = ids["session_state"]
    party_id = ids["party_id"]
    match_id = ids["match_id"]
    shard = ids["shard"]

    conversations = get_all_conversations()
    ares_cids = {}
    for conv in conversations:
        ctype = conv["type"]
        if ctype in ("coregame", "pregame", "party"):
            ares_cids[ctype] = conv

    if preferred_type == "team":
        if "coregame" in ares_cids:
            conv = ares_cids["coregame"]
            cid = conv["cid"]
            team = cid.split("@")[0].rsplit("-", 1)[-1]
            if team in ("blue", "all"):
                log.info("Selected team chat: cid=%s", cid)
                _cache["cid"] = cid
                _cache["chat_type"] = "coregame"
                _cache["expires"] = now + CACHE_TTL
                return {"cid": cid, "type": conv["chat_type"], "chat_type": "coregame"}
        if match_id and session_state in ("INGAME", "PREGAME"):
            cid = construct_team_chat_cid(match_id, shard)
            log.info("Constructed team chat CID: %s", cid)
            _cache["cid"] = cid
            _cache["chat_type"] = "coregame"
            _cache["expires"] = now + CACHE_TTL
            return {"cid": cid, "type": "groupchat", "chat_type": "coregame"}
        if "pregame" in ares_cids:
            conv = ares_cids["pregame"]
            log.info("Selected pregame chat: cid=%s", conv["cid"])
            _cache["cid"] = conv["cid"]
            _cache["chat_type"] = "pregame"
            _cache["expires"] = now + CACHE_TTL
            return {"cid": conv["cid"], "type": conv["chat_type"], "chat_type": "pregame"}

    conversations = get_all_conversations()
    ares_cids = {}
    for conv in conversations:
        ctype = conv["type"]
        if ctype in ("coregame", "pregame", "party"):
            ares_cids[ctype] = conv

    if preferred_type == "team":
        for ctype in ["coregame", "pregame"]:
            if ctype in ares_cids:
                conv = ares_cids[ctype]
                cid = conv["cid"]
                if ctype == "coregame":
                    team = cid.split("@")[0].rsplit("-", 1)[-1]
                    if team not in ("blue", "all"):
                        continue
                log.info("Selected chat: cid=%s type=%s (preferred=%s)", cid, ctype, preferred_type)
                _cache["cid"] = cid
                _cache["chat_type"] = ctype
                _cache["expires"] = now + CACHE_TTL
                return {"cid": cid, "type": conv["chat_type"], "chat_type": ctype}
        if match_id and session_state in ("INGAME", "PREGAME"):
            if session_state == "INGAME":
                cid = construct_team_chat_cid(match_id, shard)
                log.info("Constructed team chat CID from match_id: %s", cid)
            else:
                cid = construct_pregame_chat_cid(match_id, shard)
                log.info("Constructed pregame chat CID from match_id: %s", cid)
            _cache["cid"] = cid
            _cache["chat_type"] = "coregame" if session_state == "INGAME" else "pregame"
            _cache["expires"] = now + CACHE_TTL
            return {"cid": cid, "type": "groupchat", "chat_type": _cache["chat_type"]}
    elif preferred_type == "party":
        if "party" in ares_cids:
            conv = ares_cids["party"]
            cid = conv["cid"]
            log.info("Selected party chat: cid=%s", cid)
            _cache["cid"] = cid
            _cache["chat_type"] = "party"
            _cache["expires"] = now + CACHE_TTL
            return {"cid": cid, "type": conv["chat_type"], "chat_type": "party"}
        if party_id:
            cid = construct_party_chat_cid(party_id, shard)
            log.info("Constructed party chat CID: %s", cid)
            _cache["cid"] = cid
            _cache["chat_type"] = "party"
            _cache["expires"] = now + CACHE_TTL
            return {"cid": cid, "type": "groupchat", "chat_type": "party"}

    if preferred_type == "auto":
        for ctype in ["coregame", "pregame", "party"]:
            if ctype in ares_cids:
                conv = ares_cids[ctype]
                cid = conv["cid"]
                if ctype == "coregame":
                    team = cid.split("@")[0].rsplit("-", 1)[-1]
                    if team not in ("blue", "all"):
                        continue
                log.info("Auto-selected chat: cid=%s type=%s", cid, ctype)
                _cache["cid"] = cid
                _cache["chat_type"] = ctype
                _cache["expires"] = now + CACHE_TTL
                return {"cid": cid, "type": conv["chat_type"], "chat_type": ctype}

        if match_id and session_state in ("INGAME", "PREGAME"):
            if session_state == "INGAME":
                cid = construct_team_chat_cid(match_id, shard)
            else:
                cid = construct_pregame_chat_cid(match_id, shard)
            log.info("Auto: using constructed match CID: %s", cid)
            _cache["cid"] = cid
            _cache["chat_type"] = "coregame" if session_state == "INGAME" else "pregame"
            _cache["expires"] = now + CACHE_TTL
            return {"cid": cid, "type": "groupchat", "chat_type": _cache["chat_type"]}

        if party_id:
            cid = construct_party_chat_cid(party_id, shard)
            log.info("Auto: using constructed party CID: %s", cid)
            _cache["cid"] = cid
            _cache["chat_type"] = "party"
            _cache["expires"] = now + CACHE_TTL
            return {"cid": cid, "type": "groupchat", "chat_type": "party"}

    for conv in conversations:
        if conv["type"] == "dm":
            cid = conv["cid"]
            log.info("Fallback to DM: cid=%s", cid)
            _cache["cid"] = cid
            _cache["chat_type"] = "dm"
            _cache["expires"] = now + CACHE_TTL
            return {"cid": cid, "type": conv["chat_type"], "chat_type": "dm"}

    log.warning("No conversations found")
    return None


def send_chat_message(message: str, preferred_type: str = "auto") -> dict:
    chat = get_team_chat_cid(preferred_type)
    if not chat:
        return {
            "success": False,
            "error": "No active chat conversation found. Are you in a game or party?",
        }

    max_retries = 3
    retry_delay = 1.0

    for attempt in range(1, max_retries + 1):
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

        if attempt < max_retries:
            log.info("Send attempt %d failed, retrying in %.1fs...", attempt, retry_delay)
            time.sleep(retry_delay)
            _cache["all_conversations"] = None
            _cache["all_conversations_expires"] = 0

    return {
        "success": False,
        "error": f"Failed to send to {chat['chat_type']} chat. Conversation may not be active yet.",
    }


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

    start_ws_listener()

    verbose = os.environ.get("BRIDGE_WS_VERBOSE", "").lower() in ("1", "true", "yes")
    if verbose:
        log.info("WebSocket verbose logging ENABLED (BRIDGE_WS_VERBOSE=1)")

    print()
    print("  ========================================================")
    print("  |        VALORANT PHONE CHAT BRIDGE v1.3              |")
    print("  |======================================================|")
    print("  |                                                      |")
    print("  |   On your phone, open:                               |")
    print(f"  |   http://{local_ip}:8080              |")
    print("  |                                                      |")
    print("  |   Phone & PC must be on the same WiFi network.       |")
    print("  |   Valorant must be running and in a match.           |")
    print("  |                                                      |")
    print("  |   Logs: bridge.log (full, includes WS events)        |")
    print("  |         bridge-run.log (startup + console)          |")
    print("  |                                                      |")
    print("  |   Press Ctrl+C to stop.                              |")
    print("  ========================================================")
    print()

    app.run(host="0.0.0.0", port=8080, debug=False)
