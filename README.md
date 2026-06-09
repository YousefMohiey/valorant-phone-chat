# Valorant Phone Chat Bridge

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.1-000000?logo=flask&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey?logo=windows)

Send Valorant chat messages from your phone. This Python/Flask bridge reads Riot's lockfile to get local API credentials, auto-discovers the team chat conversation ID, and proxies messages from a phone web UI to Valorant's internal HTTP API. Runs on the same Windows PC as Valorant. Phone connects via local WiFi.

## Architecture

```
+-----------------+
|     Phone       |
|    (WiFi)       |
+--------+--------+
         |  HTTP POST
         |  Port 8080
         v
+-------------------------+
|     Flask Bridge        |
|     (Your PC)           |
|                         |
|  - Reads lockfile       |
|  - Discovers chat CID   |
|  - Proxies to API       |
+----------+--------------+
           |  HTTPS
           |  127.0.0.1:PORT
           v
+-------------------------+
|   Valorant Client       |
|   (Local API)           |
+-------------------------+
```

## Features

- **Automatic CID Discovery** - Finds your team chat conversation ID without manual configuration
- **Multiple Chat Modes** - Supports team chat, pregame (agent select), and party chat
- **Quick Preset Messages** - One-tap common callouts like "rush A", "need backup", "nice"
- **Web-Based Interface** - Works on any phone browser, no app installation needed
- **Real-Time Status** - Shows connection state and current chat mode
- **One-Click Launcher** - `run.bat` auto-detects IP, installs deps, configures firewall, starts the bridge
- **Vanguard Safe** - Uses Valorant's own internal API, no memory injection or keyboard simulation

## Screenshots

The web interface provides a mobile-optimized layout with a text input field, send button, and a row of preset message buttons. Open it on your phone and start typing.

## Installation

### Prerequisites

- Windows 10/11
- Python 3.11 or higher
- Valorant installed and running
- Phone and PC on the same WiFi network

### Setup & Launch

**One-click (recommended):**

Double-click `run.bat`. It auto-detects everything — Python version, IP address, dependencies, firewall rules — and starts the server. No terminal needed.

**Manual:**

```bash
pip install -r requirements.txt
python server.py
```

**Logs:**

Both `run.bat` and `server.py` write logs to `bridge-run.log` and `bridge.log` respectively in the project folder. If something crashes, check these files for the full error output.

## Usage

1. **Start Valorant** and join a match

2. **Double-click `run.bat`** (or run `python server.py`)

3. **Open your phone browser** to the URL shown in the console (`http://192.168.x.x:8080`)

4. **Send messages** — type or tap a preset. Messages appear in Valorant team chat instantly.

### Chat Modes

The bridge automatically detects available chat modes:

- **Team Chat** - In-match communication with your team
- **Pregame Chat** - Agent select lobby chat
- **Party Chat** - Pre-made group chat (if in a party)

The server prioritizes team chat during matches, falls back to pregame, then party chat.

## Technical Details

### How It Works

Valorant runs a local HTTP API that the game client uses for internal communication. This API is accessible on `127.0.0.1` using credentials stored in a lockfile.

**Lockfile Location:**

```
%LocalAppData%\Riot Games\Riot Client\Config\lockfile
```

The lockfile contains:

- Process name
- Process ID
- Port number
- Password
- Protocol (https)

**API Endpoint:**

```
POST https://127.0.0.1:{PORT}/chat/v6/messages
```

**Authentication:**

Basic auth using `riot:{PASSWORD}` encoded in base64. SSL verification is disabled because the local API uses a self-signed certificate.

**Message Flow:**

1. Bridge reads lockfile and extracts credentials
2. Queries `chat/v6/conversations/ares-coregame` to find team chat CID
3. Receives message from phone via HTTP POST
4. Forwards message to Valorant's chat API with the discovered CID
5. Valorant processes the message as if sent from the game client

### Why This Is Safe

This approach uses Valorant's own internal API, the same one the game client uses. There is:

- No memory reading or writing
- No DLL injection
- No keyboard or mouse simulation
- No process hooking
- No modification of game files

The bridge communicates with Valorant exactly as the official client does. Riot's anti-cheat (Vanguard) sees legitimate API calls from a local process.

## FAQ

### Can I get banned for using this?

This tool uses Valorant's local API, which Riot provides for the game client itself. It doesn't inject code, modify memory, or simulate input. That said, this is an unofficial use of the API. Use at your own discretion.

### Does it work in all chat modes?

It supports team chat (in-match), pregame chat (agent select), and party chat. The bridge detects which mode is available and routes messages accordingly.

### Why won't my phone connect?

Check these:

- Phone and PC are on the same WiFi network
- Windows Firewall allows inbound connections on port 8080
- You entered the correct IP address (run `ipconfig` to verify)
- Valorant is running and you are in a match or lobby

### Do I need to restart the server for each match?

No. The server runs continuously and discovers new chat sessions when you join different matches.

### Can multiple people use this at the same time?

Yes. Anyone on your WiFi network can access the web interface and send messages. All messages go through your Valorant client.

### Why does the browser show a security warning?

The connection between your phone and PC uses HTTP, not HTTPS. This is fine on your local network. The bridge uses HTTPS when talking to Valorant's local API, but that stays on your machine.

## Security Considerations

This bridge runs on your local network and accepts HTTP requests from any device on that network:

- Only devices on your WiFi can access the bridge
- No authentication is required to send messages
- Messages are sent through your Valorant account
- The bridge only runs when you explicitly start it

If you are on a shared or public network, others on the same network could send messages through your bridge. Run it only on trusted networks.

## Disclaimer

This project is not affiliated with, endorsed by, or connected to Riot Games or Valorant. It uses undocumented local API endpoints that Riot provides for the game client's internal use.

The local API is not officially supported for third-party applications. Riot could change or restrict access to these endpoints in future updates without notice.

Use this tool at your own risk. The authors are not responsible for any consequences that may arise from using this software.

## License

MIT License. See LICENSE file for details.

---

Built for the Valorant community.
