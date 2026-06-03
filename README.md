# Read-only iMessage MCP server

Gives an assistant **read-only** access to your macOS Messages so it can help you
read, search, and analyze texts. It **cannot send, edit, or delete** anything —
every database connection is opened with SQLite `mode=ro`. You send replies yourself.

## What it reads
- `~/Library/Messages/chat.db` — your iMessage/SMS history (read-only)
- `~/Library/Application Support/AddressBook/.../AddressBook-v22.abcddb` — to turn
  phone numbers/emails into contact names (read-only, best effort)

It never writes to, copies, or transmits these anywhere — it just answers queries
from whichever Claude client is connected.

## Tools
| Tool | What it does |
|------|--------------|
| `list_conversations` | Most recently active threads + last-message preview |
| `list_unreplied` | Threads where the other person texted last (awaiting your reply) |
| `get_conversation` | Recent message history with a person or group (fuzzy name/phone/email match) |
| `search_messages` | Full-text search across messages, optionally scoped to one contact |
| `get_recent_messages` | Inbox-style view of recent messages across all threads (optional `hours` window) |

## Requirements
- [`uv`](https://docs.astral.sh/uv/) installed (handles the Python deps automatically).
- **Full Disk Access** for whatever app runs the server, because `chat.db` is protected:
  - **Claude Code (CLI):** the *terminal app* you launch `claude` from (Terminal, iTerm, etc.).
  - **Claude Desktop:** the **Claude.app** itself.
  Grant it in **System Settings → Privacy & Security → Full Disk Access**, then restart the app.

## How it's wired up
- **Claude Code:** registered at user scope (`~/.claude.json`).
  Remove with `claude mcp remove imessage-readonly`.
- **Claude Desktop:** added under `mcpServers` in
  `~/Library/Application Support/Claude/claude_desktop_config.json`
  (a timestamped `.bak` of the original sits next to it). Restart the app to load it.

## Notes
- On recent macOS the message text is stored in a binary `attributedBody` blob rather
  than the `text` column; `server.py` decodes it (handles emoji/accents).
- Timestamps are converted from Apple absolute time (nanoseconds since 2001-01-01).
- Override the database path with the `IMESSAGE_DB` environment variable if needed.
