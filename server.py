# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0"]
# ///
"""
Read-only iMessage MCP server.

Exposes read-only tools over the macOS Messages database (~/Library/Messages/chat.db)
so an assistant can help read, search, and analyze your texts. It CANNOT send,
edit, delete, or modify anything — every database connection is opened read-only.

Requires the host process (Terminal / Claude app) to have Full Disk Access.
"""

import os
import re
import sqlite3
import datetime as dt
from contextlib import contextmanager

from mcp.server.fastmcp import FastMCP

# --- Configuration ---------------------------------------------------------

CHAT_DB = os.path.expanduser(
    os.environ.get("IMESSAGE_DB", "~/Library/Messages/chat.db")
)
ADDRESSBOOK_GLOB = os.path.expanduser(
    "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb"
)
# Apple absolute time epoch: 2001-01-01 00:00:00 UTC
MAC_EPOCH = 978307200

mcp = FastMCP("imessage-readonly")


# --- Database access (strictly read-only) ----------------------------------

@contextmanager
def open_ro(path):
    """Open a SQLite database read-only. Never writes."""
    # mode=ro guarantees no writes; we still see WAL-committed rows.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        yield conn
    finally:
        conn.close()


def mac_time_to_dt(value):
    """Convert a Messages `date` (nanoseconds, or seconds on old rows) to local datetime."""
    if value is None:
        return None
    secs = value / 1_000_000_000 if value > 1_000_000_000_000 else value
    try:
        return dt.datetime.fromtimestamp(secs + MAC_EPOCH)
    except (OSError, OverflowError, ValueError):
        return None


def fmt_time(value):
    d = mac_time_to_dt(value)
    return d.strftime("%Y-%m-%d %H:%M") if d else "?"


# --- attributedBody decoding -----------------------------------------------
# On recent macOS the message text lives in a binary `typedstream` blob rather
# than the `text` column. The first NSString in the stream is the message body.

def decode_attributed_body(blob):
    if not blob:
        return None
    idx = blob.find(b"NSString")
    if idx == -1:
        idx = blob.find(b"NSMutableString")
    if idx == -1:
        return None
    p = blob.find(b"\x2b", idx)  # '+' marks the start of the length-prefixed C-string
    if p == -1 or p + 1 >= len(blob):
        return None
    p += 1
    length = blob[p]
    p += 1
    if length == 0x81:  # next 2 bytes are a little-endian length
        length = int.from_bytes(blob[p:p + 2], "little")
        p += 2
    elif length == 0x82:  # next 4 bytes are a little-endian length
        length = int.from_bytes(blob[p:p + 4], "little")
        p += 4
    text = blob[p:p + length].decode("utf-8", errors="replace").strip()
    return text or None


def message_text(text, attributed_body):
    """Best message text: prefer the plain column, fall back to the blob."""
    if text and text.strip():
        return text.strip()
    return decode_attributed_body(attributed_body)


# --- Contact name resolution (read-only, best effort) -----------------------

def _norm_phone(s):
    digits = re.sub(r"\D", "", s or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _full_name(first, last, org, nick):
    name = " ".join(p for p in (first, last) if p) or nick or org
    return name.strip() if name else None


def load_contacts():
    """Map normalized phone / lowercased email -> contact name. Empty on failure."""
    import glob

    mapping = {}
    for path in glob.glob(ADDRESSBOOK_GLOB):
        try:
            with open_ro(path) as cdb:
                for num, first, last, org, nick in cdb.execute(
                    "SELECT p.ZFULLNUMBER, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION, r.ZNICKNAME "
                    "FROM ZABCDPHONENUMBER p JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK "
                    "WHERE p.ZFULLNUMBER IS NOT NULL"
                ):
                    name = _full_name(first, last, org, nick)
                    if name:
                        mapping.setdefault(_norm_phone(num), name)
                for email, first, last, org, nick in cdb.execute(
                    "SELECT e.ZADDRESS, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION, r.ZNICKNAME "
                    "FROM ZABCDEMAILADDRESS e JOIN ZABCDRECORD r ON e.ZOWNER = r.Z_PK "
                    "WHERE e.ZADDRESS IS NOT NULL"
                ):
                    name = _full_name(first, last, org, nick)
                    if name:
                        mapping.setdefault(email.strip().lower(), name)
        except sqlite3.Error:
            continue
    return mapping


# Cache contacts for the process lifetime; refresh lazily is unnecessary for a session.
_CONTACTS = None


def contacts():
    global _CONTACTS
    if _CONTACTS is None:
        _CONTACTS = load_contacts()
    return _CONTACTS


def display_for_handle(handle_id):
    """Turn a phone/email handle into a friendly name when we know it."""
    if not handle_id:
        return "Unknown"
    c = contacts()
    if "@" in handle_id:
        return c.get(handle_id.strip().lower(), handle_id)
    return c.get(_norm_phone(handle_id), handle_id)


# --- Chat/handle resolution -------------------------------------------------

def handle_map(conn):
    """ROWID -> handle id (phone/email)."""
    return {row[0]: row[1] for row in conn.execute("SELECT ROWID, id FROM handle")}


def chat_label(conn, chat_rowid, hmap):
    """A readable label for a chat: group name, or the other person's name."""
    row = conn.execute(
        "SELECT display_name, chat_identifier FROM chat WHERE ROWID = ?", (chat_rowid,)
    ).fetchone()
    if not row:
        return "Unknown"
    display_name, chat_identifier = row
    if display_name and display_name.strip():
        return display_name.strip()
    parts = [
        hid for (hid,) in conn.execute(
            "SELECT h.id FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id "
            "WHERE chj.chat_id = ?", (chat_rowid,)
        )
    ]
    if parts:
        names = [display_for_handle(p) for p in parts]
        return ", ".join(names) if len(names) <= 4 else f"{', '.join(names[:4])} +{len(names) - 4}"
    return display_for_handle(chat_identifier)


def resolve_chat_ids(conn, contact, hmap):
    """Find chat ROWIDs matching a free-text contact (name, phone, email, or group)."""
    needle = contact.strip().lower()
    chat_ids = set()

    # 1) Direct match on chat display name / identifier (covers group chats).
    for (rowid,) in conn.execute(
        "SELECT ROWID FROM chat WHERE lower(coalesce(display_name,'')) LIKE ? "
        "OR lower(coalesce(chat_identifier,'')) LIKE ?",
        (f"%{needle}%", f"%{needle}%"),
    ):
        chat_ids.add(rowid)

    # 2) Match on participant handle id or resolved contact name.
    matching_handles = [
        rid for rid, hid in hmap.items()
        if needle in (hid or "").lower() or needle in display_for_handle(hid).lower()
    ]
    if matching_handles:
        placeholders = ",".join("?" * len(matching_handles))
        for (rowid,) in conn.execute(
            f"SELECT DISTINCT chat_id FROM chat_handle_join WHERE handle_id IN ({placeholders})",
            matching_handles,
        ):
            chat_ids.add(rowid)

    return chat_ids


def fetch_messages(conn, chat_ids=None, limit=40, query_bytes=None, query_like=None):
    """Fetch messages (optionally scoped to chats / matching a search), newest first."""
    sql = [
        "SELECT m.date, m.is_from_me, m.handle_id, m.text, m.attributedBody, cmj.chat_id",
        "FROM message m",
        "LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID",
    ]
    where, params = [], []
    if chat_ids:
        where.append(f"cmj.chat_id IN ({','.join('?' * len(chat_ids))})")
        params.extend(chat_ids)
    if query_bytes is not None:
        where.append("(m.text LIKE ? OR instr(m.attributedBody, ?) > 0)")
        params.extend([query_like, query_bytes])
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY m.date DESC LIMIT ?")
    params.append(limit)
    return conn.execute(" ".join(sql), params).fetchall()


def render_messages(conn, rows, hmap, show_chat=False, chronological=True):
    """Format raw message rows into readable lines."""
    if chronological:
        rows = list(reversed(rows))
    lines = []
    for date, is_from_me, handle_id, text, ab, chat_id in rows:
        body = message_text(text, ab)
        if not body:
            continue  # attachment / reaction / empty
        sender = "You" if is_from_me else display_for_handle(hmap.get(handle_id))
        prefix = f"[{fmt_time(date)} | {sender}]"
        if show_chat and chat_id is not None:
            prefix = f"[{fmt_time(date)} | {chat_label(conn, chat_id, hmap)} | {sender}]"
        lines.append(f"{prefix} {body}")
    return lines


# --- Tools (all read-only) --------------------------------------------------

@mcp.tool()
def list_conversations(limit: int = 20) -> str:
    """List your most recently active iMessage/SMS conversations.

    Returns each conversation's name, who the latest message was from, when, and a
    preview of the last message. Use this to get an overview of recent activity.

    Args:
        limit: Maximum number of conversations to return (default 20).
    """
    with open_ro(CHAT_DB) as conn:
        hmap = handle_map(conn)
        rows = conn.execute(
            "SELECT cmj.chat_id, MAX(m.date) AS last_date "
            "FROM message m JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            "GROUP BY cmj.chat_id ORDER BY last_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for chat_id, last_date in rows:
            last = conn.execute(
                "SELECT m.date, m.is_from_me, m.handle_id, m.text, m.attributedBody "
                "FROM message m JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
                "WHERE cmj.chat_id = ? ORDER BY m.date DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
            label = chat_label(conn, chat_id, hmap)
            if not last:
                continue
            date, is_from_me, handle_id, text, ab = last
            body = message_text(text, ab) or "(no text — attachment or reaction)"
            who = "You" if is_from_me else display_for_handle(hmap.get(handle_id))
            preview = body if len(body) <= 100 else body[:100] + "…"
            out.append(f"• {label}\n    last: [{fmt_time(date)} | {who}] {preview}")
        return "\n".join(out) if out else "No conversations found."


@mcp.tool()
def list_unreplied(limit: int = 20) -> str:
    """List conversations whose most recent message was from the other person.

    These are the threads currently waiting on a reply from you — useful for
    figuring out which texts still need a response.

    Args:
        limit: Maximum number of conversations to return (default 20).
    """
    with open_ro(CHAT_DB) as conn:
        hmap = handle_map(conn)
        rows = conn.execute(
            "SELECT cmj.chat_id, MAX(m.date) AS last_date "
            "FROM message m JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            "GROUP BY cmj.chat_id ORDER BY last_date DESC LIMIT ?",
            (limit * 4,),  # over-fetch, then filter to those awaiting a reply
        ).fetchall()
        out = []
        for chat_id, last_date in rows:
            last = conn.execute(
                "SELECT m.date, m.is_from_me, m.handle_id, m.text, m.attributedBody "
                "FROM message m JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
                "WHERE cmj.chat_id = ? ORDER BY m.date DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
            if not last or last[1]:  # last[1] == is_from_me; skip if you sent last
                continue
            date, is_from_me, handle_id, text, ab = last
            body = message_text(text, ab) or "(no text — attachment or reaction)"
            label = chat_label(conn, chat_id, hmap)
            who = display_for_handle(hmap.get(handle_id))
            preview = body if len(body) <= 120 else body[:120] + "…"
            out.append(f"• {label}\n    waiting since [{fmt_time(date)}] {who}: {preview}")
            if len(out) >= limit:
                break
        return "\n".join(out) if out else "You're all caught up — no conversations awaiting a reply."


@mcp.tool()
def get_conversation(contact: str, limit: int = 40) -> str:
    """Read the recent message history with a specific person or group.

    Args:
        contact: A name, phone number, email, or group name to match. Matching is
            fuzzy/substring against contact names, handles, and group titles.
        limit: Maximum number of messages to return, most recent (default 40).
    """
    with open_ro(CHAT_DB) as conn:
        hmap = handle_map(conn)
        chat_ids = resolve_chat_ids(conn, contact, hmap)
        if not chat_ids:
            return f"No conversation found matching '{contact}'. Try a phone number, email, or different spelling."
        labels = sorted({chat_label(conn, cid, hmap) for cid in chat_ids})
        rows = fetch_messages(conn, chat_ids=chat_ids, limit=limit)
        lines = render_messages(conn, rows, hmap, show_chat=len(chat_ids) > 1)
        header = f"Conversation with {', '.join(labels)} (showing up to {limit} most recent):"
        return header + "\n" + ("\n".join(lines) if lines else "(no text messages found)")


@mcp.tool()
def search_messages(query: str, limit: int = 30, contact: str = "") -> str:
    """Search the text of your messages for a word or phrase.

    Args:
        query: Text to search for (case-insensitive substring match).
        limit: Maximum number of matching messages to return (default 30).
        contact: Optional name/phone/email/group to restrict the search to one conversation.
    """
    if not query.strip():
        return "Please provide a non-empty search query."
    with open_ro(CHAT_DB) as conn:
        hmap = handle_map(conn)
        chat_ids = None
        if contact.strip():
            chat_ids = resolve_chat_ids(conn, contact, hmap)
            if not chat_ids:
                return f"No conversation found matching '{contact}'."
        rows = fetch_messages(
            conn,
            chat_ids=chat_ids,
            limit=limit,
            query_bytes=query.encode("utf-8"),
            query_like=f"%{query}%",
        )
        lines = render_messages(conn, rows, hmap, show_chat=True, chronological=False)
        if not lines:
            return f"No messages found matching '{query}'."
        return f"{len(lines)} match(es) for '{query}' (newest first):\n" + "\n".join(lines)


@mcp.tool()
def get_recent_messages(limit: int = 40, hours: int = 0) -> str:
    """Show the most recent messages across all conversations (an inbox-style view).

    Args:
        limit: Maximum number of messages to return (default 40).
        hours: If > 0, only include messages from the last N hours.
    """
    with open_ro(CHAT_DB) as conn:
        hmap = handle_map(conn)
        params = []
        where = ""
        if hours and hours > 0:
            cutoff_secs = dt.datetime.now().timestamp() - hours * 3600
            cutoff_mac_ns = int((cutoff_secs - MAC_EPOCH) * 1_000_000_000)
            where = "WHERE m.date >= ?"
            params.append(cutoff_mac_ns)
        params.append(limit)
        rows = conn.execute(
            "SELECT m.date, m.is_from_me, m.handle_id, m.text, m.attributedBody, cmj.chat_id "
            "FROM message m LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            f"{where} ORDER BY m.date DESC LIMIT ?",
            params,
        ).fetchall()
        lines = render_messages(conn, rows, hmap, show_chat=True)
        scope = f" from the last {hours}h" if hours and hours > 0 else ""
        if not lines:
            return f"No recent messages{scope}."
        return f"Recent messages{scope} (oldest→newest):\n" + "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
