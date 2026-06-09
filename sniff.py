"""
WebSocket Sniffer for Valorant Local API
==========================================
Run this alongside the bridge. It connects to Valorant's local WebSocket
and logs ALL events. Send a team/party chat message in-game and see which
endpoints and data the game uses.
"""
import base64
import json
import os
import ssl
import socket
import time
from hashlib import sha1
from base64 import b64encode

LOG_FILE = "sniff.log"

def read_lockfile():
    path = os.path.expandvars(r"%LocalAppData%\Riot Games\Riot Client\Config\lockfile")
    with open(path, "r") as f:
        parts = f.read().strip().split(":")
    return {"port": parts[2], "password": parts[3]}


def ws_connect(host, port, path="/", auth_header=None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ssl_sock = ctx.wrap_socket(sock, server_hostname=host)
    ssl_sock.connect((host, int(port)))
    
    key = b64encode(os.urandom(16)).decode()
    headers = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if auth_header:
        headers.append(f"Authorization: Basic {auth_header}")
    headers.append("")
    headers.append("")
    
    request = "\r\n".join(headers)
    ssl_sock.send(request.encode())
    response = ssl_sock.recv(4096)
    if b"101" not in response:
        raise Exception(f"WebSocket handshake failed: {response}")
    return ssl_sock


def ws_send(sock, text):
    data = text.encode()
    frame = bytearray()
    frame.append(0x81)  # FIN + text opcode
    length = len(data)
    if length < 126:
        frame.append(length | 0x80)  # masked
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
    log(f"[SENT] {text[:100]}")


def ws_recv(sock):
    header = sock.recv(2)
    if len(header) < 2:
        return None
    
    opcode = header[0] & 0x0F
    if opcode == 0x8:
        return None
    
    masked = header[1] & 0x80
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
    
    mask_key = None
    if masked:
        mask_key = sock.recv(4)
        if len(mask_key) < 4:
            return None
    
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            return None
        payload += chunk
    
    if mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    
    return payload


def main():
    lockfile = read_lockfile()
    log("=" * 60)
    log("VALORANT CHAT SNIFFER")
    log("=" * 60)
    log(f"Port: {lockfile['port']}")
    log("")
    log("Connecting to WebSocket...")
    
    auth = base64.b64encode(f"riot:{lockfile['password']}".encode()).decode()
    
    try:
        sock = ws_connect("127.0.0.1", lockfile["port"], auth_header=auth)
        log("Connected!")
        log("")
        log("SUBSCRIBE to all events. Now send a chat message in-game...")
        log("(Team chat / Party chat / DM - anything)")
        log("=" * 60)
        log("")
        
        # Subscribe to chat events
        log("Subscribing to events...")
        ws_send(sock, json.dumps([5, "OnJsonApiEvent"]))
        time.sleep(0.1)
        ws_send(sock, json.dumps([5, "OnJsonApiEvent_chat_v4_presences"]))
        time.sleep(0.1)
        ws_send(sock, json.dumps([5, "OnJsonApiEvent_chat_v6_messages"]))
        time.sleep(0.1)
        log("Subscriptions sent.")
        
        log("Listening for events. Press Ctrl+C to stop.")
        
        while True:
            data = ws_recv(sock)
            if data is None:
                break
            try:
                text = data.decode("utf-8")
                log_json(text)
            except (UnicodeDecodeError, Exception) as e:
                if len(data) < 200:
                    log(f"[RAW] {data}")
    except Exception as e:
        log(f"ERROR: {e}")
    
    log("Done.")


def log_json(text):
    try:
        data = json.loads(text)
        formatted = json.dumps(data, indent=2)
        for line in formatted.split("\n"):
            log(f"[WS] {line}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        if len(text) < 200:
            log(f"[RAW] {text}")


def log(msg):
    timestamp = time.strftime("%H:%M:%S")
    line = f"{timestamp} {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    log(f"\n--- Sniffer started at {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
    main()
