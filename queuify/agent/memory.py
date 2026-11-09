"""What the model sees besides the system prompt: a state snapshot and a recent transcript.
Both are rebuilt from SQLite every turn; nothing is kept server-side."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from queuify import observe
from queuify.domain import pending
from queuify.swiggy.names import short_name
from queuify.domain.queue import item_label, members, name_of, owner_names, queued_lines, queued_total_paise, rupees

IST = timezone(timedelta(hours=5, minutes=30))  # a fixed offset: slim Docker images ship no tz database
WINDOW_MESSAGES = 20
WINDOW_AGE = timedelta(hours=12)
MAX_QUESTIONS = 10
SHOWN_OPTIONS = 4  # for old pending rows that have no "picked"
REPLY_TEXT_CAP = 120


def _age(added_at: str, now: datetime) -> str:
    delta = now - datetime.fromisoformat(added_at)
    hours = int(delta.total_seconds() // 3600)
    if hours >= 48:
        return f"{hours // 24}d"
    return f"{hours}h" if hours else f"{int(delta.total_seconds() // 60)}m"


def build_snapshot(conn: sqlite3.Connection, sender: int, now: datetime, free_delivery_min: int = 199) -> str:
    kv = dict(conn.execute("SELECT key, value FROM kv").fetchall())
    out = [f"Now: {now.astimezone(IST):%a %d %b %Y %H:%M} IST",
           "Members: " + (", ".join(f"{m['display_name']} (id {m['user_id']})" for m in members(conn)) or "none")]

    lines = queued_lines(conn)
    if lines:
        out.append("Queue:")
    for l in lines:
        price = f", est {rupees(l['est_unit_price_paise'] * l['qty'])}" if l["est_unit_price_paise"] else ""
        out.append(f"  #{l['slot']} {item_label(l['name'], l['variant'], l['qty'])} for {owner_names(conn, l['id'])}"
                   f"{price}, queued {_age(l['added_at'], now)} ago")
    if not lines:
        out.append("Queue: empty")
    total = queued_total_paise(conn)
    gap = free_delivery_min * 100 - total
    out.append(f"Estimated total {rupees(total)} vs free-delivery minimum ₹{free_delivery_min}: "
               + (f"{rupees(gap)} to go" if gap > 0 else "reached"))

    out += _open_questions(conn, sender, now)

    label = json.loads(kv["address_label"])["label"] if kv.get("address_label") else "unknown"
    out.append(f"Swiggy login: {kv.get('auth_state')}; address: {label}")
    return "\n".join(out)


def _since(created_at: str, now: datetime) -> str:
    minutes = max(0, int((now - datetime.fromisoformat(created_at)).total_seconds() // 60))
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h"


def _question_line(conn: sqlite3.Connection, q: dict, sender: int, now: datetime) -> str:
    p = q["payload"]
    shown = p["options"][:p.get("picked") or SHOWN_OPTIONS]  # the agent's picks, not the "More" extras
    names = "; ".join(f"{short_name(o['name'])} {o['size']}" for o in shown)
    more = f"; (+{len(p['options']) - len(shown)} more)" if len(p["options"]) > len(shown) else ""
    mine = " (sender's)" if q["user_id"] == sender else ""
    return (f"  #{q['id']} {name_of(conn, q['user_id'])}{mine}, {_since(q['created_at'], now)} ago: "
            f"{p['question']} Options: {names}{more}")


def _open_questions(conn: sqlite3.Connection, sender: int, now: datetime) -> list[str]:
    """Every member's open variant questions, oldest first (the newest MAX_QUESTIONS if there are more)."""
    asked = pending.open_variants(conn)[-MAX_QUESTIONS:]
    return ["Open questions:"] + [_question_line(conn, q, sender, now) for q in asked] if asked else []


def transcript(conn: sqlite3.Connection, now: datetime) -> str:
    """The last 20 user messages from the past 12 h, plus every row after the oldest of them."""
    recent = conn.execute("SELECT id, ts FROM chat_memory WHERE role='user' ORDER BY id DESC LIMIT ?",
                          (WINDOW_MESSAGES,)).fetchall()
    kept = [r["id"] for r in recent if datetime.fromisoformat(r["ts"]) >= now - WINDOW_AGE]
    if not kept:
        return ""
    rows = conn.execute("SELECT role, text, to_user_id FROM chat_memory WHERE id >= ? ORDER BY id",
                        (min(kept),)).fetchall()
    return "\n".join(f"[{_tag(conn, r)}] {r['text']}" for r in rows)


def _tag(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """The role, plus who the bot was answering when the row says so."""
    if row["to_user_id"] is None:
        return row["role"]
    return f"{row['role']} → {name_of(conn, row['to_user_id'])}"


def reply_context(conn: sqlite3.Connection, replied_to: int) -> str:
    """What the bot message being replied to was: ' to Angad ("text…"; open question #19)'.
    Pieces we can't find are left out; with none, the caller says it was an earlier message."""
    row = conn.execute("SELECT text, to_user_id FROM chat_memory WHERE role='assistant' AND message_id=?",
                       (replied_to,)).fetchone()
    question = conn.execute("SELECT id, user_id, payload_json FROM pending WHERE kind='variant' AND message_id=?",
                            (replied_to,)).fetchone()
    if question and not row:  # a question is not a chat_memory row (its turn ends with a summary): quote the pending one
        row = {"text": json.loads(question["payload_json"]).get("question", ""), "to_user_id": question["user_id"]}
    order = conn.execute("SELECT id FROM orders WHERE card_message_id=?", (replied_to,)).fetchone()
    details = []
    if row and _quotable(row["text"]):
        text = " ".join(row["text"].split())
        details.append('"' + (text[:REPLY_TEXT_CAP].rstrip() + "…" if len(text) > REPLY_TEXT_CAP else text) + '"')
    if question:
        details.append(f"open question #{question['id']}")
    if order:
        details.append(f"order #{order['id']}")
    who = f" to {name_of(conn, row['to_user_id'])}" if row and row["to_user_id"] is not None else ""
    return f"{who} ({'; '.join(details)})" if details else who


def _quotable(text: str) -> bool:
    return not (observe.looks_like_paste(text) or "http" in text)


def speaker_label(conn: sqlite3.Connection, name: str, user_id: int, replied_to: int | None) -> str:
    """'Shounak (id 9)', plus ', replying to the bot's message to Angad ("…"; open question #19)' for a reply to the bot."""
    label = f"{name} (id {user_id})"
    if replied_to is None:
        return label
    context = reply_context(conn, replied_to)
    return f"{label}, replying to " + (f"the bot's message{context}" if context else "an earlier bot message")


def remember(conn: sqlite3.Connection, role: str, text: str, now: datetime, user_id: int | None = None,
             *, to_user_id: int | None = None, message_id: int | None = None) -> None:
    """role is 'user' (text already tagged with the speaker), 'assistant' or 'action'.
    An assistant row also stores who it answered and the Telegram message id it was sent as."""
    conn.execute("INSERT INTO chat_memory (ts, role, user_id, text, to_user_id, message_id) VALUES (?, ?, ?, ?, ?, ?)",
                 (now.isoformat(), role, user_id, text, to_user_id, message_id))
