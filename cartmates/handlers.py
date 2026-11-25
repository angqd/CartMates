import html
import json
import logging
import re
import sqlite3
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from cartmates import observe
from cartmates.agent import memory
from cartmates.agent.llm import LLM
from telegram.error import NetworkError

from cartmates.agent.errors import TurnFailedAfterChanges, done_so_far
from cartmates.agent.loop import run_turn
from cartmates.domain import address, nudge, orders, pending, queue
from cartmates.domain import group as group_mod
from cartmates.domain.orders import OrderError
from cartmates.domain.queue import QueueError
from cartmates.executor.order_executor import OrderExecutor
from cartmates.store import db
from cartmates.swiggy import catalog
from cartmates.swiggy.auth import SwiggyAuth
from cartmates.swiggy.client import SwiggyClient
from cartmates.swiggy.errors import LoggedOut, LoggedOutAfterChanges, LoginError, SwiggyToolError, SwiggyUnavailable
from cartmates.telegram.events import CallbackTap, CleanupPending, Event, Message, Recover
from cartmates.telegram.outbox import Buttons, Outbox

log = logging.getLogger(__name__)

# (command, description, in the menu?). The menu and /help show the first five; the rest still work when typed.
COMMANDS = [
    ("help", "what I am and how to talk to me", True),
    ("list", "show the queue", True),
    ("order", "order now (everything or just yours)", True),
    ("address", "switch the delivery address", True),
    ("status", "Swiggy login and delivery address", True),
    ("track", "[id] live delivery status", False),
    ("history", "[id] past orders", False),
    ("login", "log in to Swiggy", False),
    ("resolve", "<id> placed|failed, clear an unknown order", False),
]
HIDDEN_LINE = "Also: /track, /history, /login, /resolve"
REMOVED = {
    "add": 'Just @mention me in plain words, e.g. "add 2 milk".',
    "remove": 'Just @mention me in plain words, e.g. "remove the eggs".',
    "search": 'Just @mention me and ask in plain words, e.g. "what instant coffee is there?".',
}


def menu_commands() -> list[tuple[str, str]]:
    """What the Telegram "/" menu shows: (name, description) for the visible commands."""
    return [(name, desc.split("> ")[-1]) for name, desc, shown in COMMANDS if shown]

LOGIN_TTL = timedelta(minutes=10)
PARKED_REPLY = "I need a Swiggy login first; I'll finish your request after."
UNAVAILABLE = orders.UNAVAILABLE
NETWORK_NOTHING_CHANGED = "Something went wrong (connection problem?), nothing was changed. Try again."
BARE_MENTION = "Tell me what you need, e.g. \"add 2 milk\" or \"what's in the queue?\". /help shows more."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _address_label(a: dict) -> str:
    return a.get("addressTag") or a.get("addressCategory") or "Address"


WRONG_PASTE = (
    "That's the Swiggy sign-in page, not the final link. Telegram's own browser hides the page you need. "
    "Send /login again, tap the link to copy it, and open it in Safari or Chrome instead. "
    "Log in, and when the page fails to load, copy its address and send it here.\n"
    "Or do the login on a computer: open the link there, and paste the address of the page that fails to load."
)

_looks_like_paste = observe.looks_like_paste  # shared with tracing, which must never record a paste


class Handlers:
    def __init__(self, conn: sqlite3.Connection, outbox: Outbox, group: "int | group_mod.Group",
                 auth: SwiggyAuth | None = None, swiggy: SwiggyClient | None = None,
                 llm: LLM | None = None,
                 free_delivery_min: int = 199, llm_missing: str = "OPENAI_API_KEY",
                 price_tolerance: float = 0.05, upi_app: str = "gpay://upi/",
                 checkout_timeout_s: float = 45.0):
        self.conn = conn
        self.outbox = outbox
        self.group = group_mod.wrap(group)  # the live id is self.group_id
        self.auth = auth
        self.swiggy = swiggy
        self.llm = llm
        self.free_delivery_min = free_delivery_min
        self.llm_missing = llm_missing  # which setting to name when the assistant is off
        self.executor = OrderExecutor(conn, outbox, swiggy, price_tolerance,
                                       upi_app=upi_app, checkout_timeout_s=checkout_timeout_s) if swiggy else None
        self.resubmit = None  # set to lane.submit once the lane exists
        self._replaying: set[int] = set()  # message ids already logged before they were parked

    def _address(self) -> str | None:
        return address.current(self.conn)

    @property
    def group_id(self) -> int | None:
        """Read at use time: a supergroup upgrade moves the registry row (DESIGN 7.4)."""
        return self.group.current()

    async def handle(self, event: Event) -> None:
        if event.chat_id != self.group_id:
            return
        if isinstance(event, CleanupPending):
            deleted = pending.delete_expired(self.conn)
            if deleted:
                log.info("deleted %d expired question(s)", deleted)
            return
        if isinstance(event, Recover):
            await self._recover(event)
            return
        if isinstance(event, CallbackTap):
            await self._on_tap(event)
            return
        paste = _looks_like_paste(event.text)
        self._record(event, redact=paste)
        turn = self.swiggy.turn() if self.swiggy else nullcontext()
        async with turn:
            try:
                if paste and observe.looks_like_wrong_paste(event.text):
                    text = WRONG_PASTE
                elif paste:
                    login = self._pending_login()
                    if not login:
                        text = "No login is waiting (or it expired). Send /login to start one."
                    else:
                        await self._finish_login(event, login)
                        return
                else:
                    before = self._version()
                    text = await self._reply(event)
                    if text:  # the reply goes out before any nudge card
                        await self.outbox.send(event.chat_id, text, reply_to=event.message_id)
                        text = None
                    await self._nudge(before, event.chat_id, event.user_id)
            except LoggedOutAfterChanges as e:
                # Replaying would repeat what was already done, so nothing is parked.
                text = await self._logged_out_partway(event, e.actions)
            except TurnFailedAfterChanges as e:
                await self._say_quietly(event, self._failed_partway(e))
                return
            except LoggedOut:
                try:
                    await self._park(event)
                    return
                except SwiggyUnavailable:  # could not even register for a login link
                    text = UNAVAILABLE
            except SwiggyUnavailable:
                text = UNAVAILABLE
        if text:  # the agent sends its own reply
            await self.outbox.send(event.chat_id, text, reply_to=event.message_id)

    def _version(self) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key='queue_version'").fetchone()
        return row[0] if row else None

    async def _nudge(self, before: str | None, chat_id: int, user_id: int) -> None:
        """Free-delivery check, only if this event changed the queue. Inside the caller's Swiggy turn."""
        if self._version() == before:
            return
        await nudge.maybe_nudge(self.conn, self.outbox, self.swiggy, self._address(), chat_id, user_id,
                                self.free_delivery_min, logged_in=not self.auth or self.auth.logged_in())

    def _record(self, event: Message, redact: bool = False) -> None:
        self.conn.execute(
            "INSERT INTO members (user_id, display_name, joined_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name",
            (event.user_id, event.display_name, _now()),
        )
        if event.message_id in self._replaying:  # a parked event coming back; already logged
            self._replaying.discard(event.message_id)
            return
        # A pasted login URL holds the auth code, which must never reach SQLite.
        text = "[login url redacted]" if redact else event.text
        queue.log_event(self.conn, event.user_id, "message", {"text": text})

    async def _reply(self, event: Message) -> str | None:
        if not event.is_command:
            return await self._agent(event, event.text)
        match event.command:
            case "help":
                return self._help()
            case "list":
                return self._list()
            case "status":
                return await self._status()
            case "login":
                return await self._login(event)
            case "add" | "remove" | "search":
                return REMOVED[event.command]
            case "order":
                return await self._order(event)
            case "history":
                return self._history(event.args)
            case "resolve":
                return self._resolve(event)
            case "address":
                return await self._address_cmd(event)
            case "track":
                return await self._track(event)
            case _:
                return f"I don't know /{event.command} yet. Try /help."

    def _help(self) -> str:
        lines = ["I keep your household's grocery queue and order it from Swiggy Instamart. "
                 "Just @mention me in plain words, e.g. \"2 amul taaza for me and rohan\", "
                 "\"make the milk 3\", \"drop the curd\", \"where is my order?\".", "", "Commands:"]
        lines += [f"/{name} - {desc}" for name, desc in menu_commands()]
        lines += ["", HIDDEN_LINE]
        return "\n".join(lines)

    # ---- the agent ----

    async def _agent(self, event: Message, text: str, usage: str = BARE_MENTION) -> str | None:
        if not re.sub(r"@\w+", "", text).strip():  # nothing but a mention
            return usage
        if not self.llm:
            return f"The assistant isn't configured ({self.llm_missing})."
        try:
            await run_turn(conn=self.conn, llm=self.llm, swiggy=self.swiggy,
                           outbox=self.outbox, event=event, text=text, free_delivery_min=self.free_delivery_min)
        except NetworkError as e:  # run_turn only lets this out when nothing was changed
            log.warning("agent turn failed on the network, nothing changed: %s", e)
            await self._say_quietly(event, NETWORK_NOTHING_CHANGED)
        return None

    @staticmethod
    def _failed_partway(e: TurnFailedAfterChanges) -> str:
        done = done_so_far(e.actions)
        problem = "a connection problem" if isinstance(e.cause, NetworkError) else "an error"
        return f"Done so far: {done}. Then I hit {problem}, so ask me again for the rest."

    async def _say_quietly(self, event: Message, text: str) -> None:
        """Send an error reply. If that fails too (the network is still down), log one line and stop."""
        try:
            await self.outbox.send(event.chat_id, text, reply_to=event.message_id)
        except Exception as e:
            log.warning("could not send the failure reply (%s: %s)", type(e).__name__, e)

    # ---- buttons: plain code, no model ----

    async def _on_tap(self, tap: CallbackTap) -> None:
        queue.log_event(self.conn, tap.user_id, "tap", {"data": tap.data})
        kind = tap.data.split(":")[0]
        turn = self.swiggy.turn() if self.swiggy else nullcontext()
        try:
            async with turn:
                before = self._version()
                if kind == "o":
                    await self._on_order_tap(tap)
                elif kind == "s":
                    await self._on_scope_tap(tap)
                elif kind == "a":
                    await self._on_address_tap(tap)
                else:
                    await self._on_variant_tap(tap)
                    await self._nudge(before, tap.chat_id, tap.user_id)
        except OrderError as e:
            await self.outbox.answer_tap(tap.tap_id, str(e), alert=True)
        except LoggedOut:
            await self.outbox.answer_tap(tap.tap_id, "Swiggy is logged out. Send /login, then try again.", alert=True)
        except (SwiggyUnavailable, SwiggyToolError):
            await self.outbox.answer_tap(tap.tap_id, UNAVAILABLE, alert=True)

    async def _on_order_tap(self, tap: CallbackTap) -> None:
        try:
            _, order_id, action = tap.data.split(":")
            order_id = int(order_id)
            assert action in ("c", "u", "x")
        except (ValueError, AssertionError):
            await self.outbox.answer_tap(tap.tap_id, "This button expired")
            return
        if action == "x":
            with db.transaction(self.conn):
                done = orders.cancel(self.conn, order_id, tap.user_id)
            if done:
                await self.outbox.edit(tap.chat_id, tap.message_id, f"Order #{order_id} cancelled by {tap.display_name}.")
            await self.outbox.answer_tap(tap.tap_id, None if done else "Already handled")
            return
        if not self.executor:
            await self.outbox.answer_tap(tap.tap_id, "Swiggy isn't set up.", alert=True)
            return
        # Telegram wants the tap answered quickly, so the executor answers right after its claim.
        async def answer(text: str | None) -> None:
            await self.outbox.answer_tap(tap.tap_id, text)
        await self.executor.confirm(order_id, tap, answer, "upi" if action == "u" else "cod")

    async def _on_scope_tap(self, tap: CallbackTap) -> None:
        try:
            _, pending_id, choice = tap.data.split(":")
            assert choice in ("m", "a")
            question = pending.get_scope(self.conn, int(pending_id))
        except (ValueError, AssertionError):
            question = None
        if not question:
            await self.outbox.answer_tap(tap.tap_id, "This choice expired")
            return
        if question["user_id"] != tap.user_id:
            asker = queue.name_of(self.conn, question["user_id"])
            await self.outbox.answer_tap(tap.tap_id, f"Only {asker} can pick this", alert=True)
            return
        pending.delete(self.conn, question["id"])  # a double tap finds it gone
        await self.outbox.answer_tap(tap.tap_id)
        await self.outbox.edit(tap.chat_id, tap.message_id, "Getting a quote...")
        try:
            await orders.propose(self.conn, self.outbox, self.swiggy, self._address(), tap.chat_id,
                                 tap.user_id, "mine" if choice == "m" else "all")
        except OrderError as e:
            await self.outbox.edit(tap.chat_id, tap.message_id, str(e))
            return
        except Exception:
            await self.outbox.edit(tap.chat_id, tap.message_id, "Couldn't get a quote right now. Try /order again.")
            raise
        await self.outbox.edit(tap.chat_id, tap.message_id, "Choice made: bill below.")

    async def _on_variant_tap(self, tap: CallbackTap) -> None:
        try:
            kind, pending_id, choice = tap.data.split(":")
            assert kind == "v"
            question = pending.get_variant(self.conn, int(pending_id))
        except (ValueError, AssertionError):
            question = None
        if not question:
            await self.outbox.answer_tap(tap.tap_id, "This choice expired")
            return
        if question["user_id"] != tap.user_id:
            asker = queue.name_of(self.conn, question["user_id"])
            await self.outbox.answer_tap(tap.tap_id, f"Only {asker} can pick this", alert=True)
            return
        p = question["payload"]
        if choice == "x":
            pending.delete(self.conn, question["id"])
            memory.remember(self.conn, "action",
                            f'{tap.display_name}: none of the options for "{p["question"]}"', datetime.now(timezone.utc))
            await self.outbox.edit(tap.chat_id, tap.message_id, "No problem — tell me what you want instead")
            await self.outbox.answer_tap(tap.tap_id)
            return
        if choice[:1] == "m" and choice[1:].isdigit():  # "More options": same message, next page, no model call
            await self.outbox.edit(tap.chat_id, tap.message_id, p["question"],
                                   pending.variant_buttons(question["id"], p["options"], int(choice[1:])))
            await self.outbox.answer_tap(tap.tap_id)
            return
        if not (choice.isdigit() and int(choice) < len(p["options"])):
            await self.outbox.answer_tap(tap.tap_id, "This choice expired")
            return
        try:
            # Deleting the question and adding the line together: a double tap can't add twice.
            with db.transaction(self.conn):
                pending.delete(self.conn, question["id"])
                slot, text = queue.add_line(self.conn, p["options"][int(choice)], p["qty"], p["owners"],
                                               p["original_text"], tap.user_id)
        except QueueError as e:
            await self.outbox.answer_tap(tap.tap_id, str(e), alert=True)
            return
        memory.remember(self.conn, "action", f"added #{slot} {text}", datetime.now(timezone.utc))
        await self.outbox.edit(tap.chat_id, tap.message_id, f"✅ {text}")
        await self.outbox.answer_tap(tap.tap_id)

    # ---- restart recovery (DESIGN 7.4) ----

    async def _recover(self, job: Recover) -> None:
        """Settle the orders that were in flight when the bot stopped. Runs inside a Swiggy turn like any event."""
        if not self.executor:
            return
        logged_in = not self.auth or self.auth.logged_in()
        async with self.swiggy.turn():
            for order_id in job.unknown:
                try:
                    await self.executor.recover_unknown(order_id, job.chat_id, logged_in)
                except Exception:  # one bad order must not hide the others
                    log.exception("recovery of order #%s failed", order_id)
        for order_id in job.failed:
            try:
                await self.executor.recover_failed(order_id, job.chat_id)
            except Exception:
                log.exception("recovery of order #%s failed", order_id)

    # ---- delivery address (DESIGN 7.2) ----

    @staticmethod
    def _address_buttons(saved: list[dict], current: str | None) -> Buttons:
        """One button per saved address. Tag or category; the area is added only to tell equal labels apart."""
        labels = [_address_label(a) for a in saved]
        rows: Buttons = []
        for a, label in zip(saved, labels):
            if labels.count(label) > 1 and a.get("area"):
                label = f"{label} · {a['area']}"
            data = f"a:{a['id']}"
            if len(data.encode()) > 64:
                log.warning("address button skipped: callback data over 64 bytes")
                continue
            mark = "✓ " if current and address.same(a["id"], current) else ""
            rows.append([(mark + label, data)])
        return rows

    @staticmethod
    def _address_message(label: str | None) -> str:
        return f"Delivery address: {label or 'unknown'}\nTap one to switch."

    async def _address_cmd(self, event: Message) -> str | None:
        if not self.swiggy:
            return "Swiggy isn't set up."
        saved = await catalog.list_addresses(self.swiggy)
        current = self._address()
        mine = next((a for a in saved if current and address.same(a["id"], current)), None)
        if not saved:
            return "No saved addresses found in the Swiggy account."
        await self.outbox.send(event.chat_id, self._address_message(_address_label(mine) if mine else None),
                               reply_to=event.message_id, buttons=self._address_buttons(saved, current))
        return None

    async def _on_address_tap(self, tap: CallbackTap) -> None:
        wanted = tap.data[2:]
        saved = await catalog.list_addresses(self.swiggy) if self.swiggy else []
        chosen = next((a for a in saved if wanted and address.same(a["id"], wanted)), None)
        if not chosen:
            await self.outbox.answer_tap(tap.tap_id, "That address is no longer saved")
            return
        old = self._address()
        if old and address.same(chosen["id"], old):
            await self.outbox.answer_tap(tap.tap_id, "Already the delivery address")
            return
        label = _address_label(chosen)
        with db.transaction(self.conn):
            address.set_current(self.conn, chosen["id"])
            self.conn.execute("DELETE FROM kv WHERE key='address_label'")
            open_cards = self.conn.execute(
                "SELECT id, card_message_id FROM orders WHERE status='proposed'").fetchall()
            for o in open_cards:
                self.conn.execute("UPDATE orders SET status='superseded', error='address changed' WHERE id=?",
                                  (o["id"],))
                queue.log_event(self.conn, "system", "order_status", {"order_id": o["id"], "status": "superseded"})
            queue.log_event(self.conn, tap.user_id, "address_changed", {"address_id": chosen["id"], "label": label})
        log.info("delivery address changed to %s by %s", label, tap.user_id)
        memory.remember(self.conn, "action", f"{tap.display_name} changed the delivery address to {label}",
                        datetime.now(timezone.utc))
        await self.outbox.answer_tap(tap.tap_id)
        for o in open_cards:
            if o["card_message_id"]:
                await self._try_edit(tap.chat_id, o["card_message_id"], "Address changed — run /order again")
        await self._try_edit(tap.chat_id, tap.message_id, self._address_message(label),
                             self._address_buttons(saved, chosen["id"]))
        await self.outbox.send(tap.chat_id, f"Delivery address is now {label} (changed by {tap.display_name})")

    async def _try_edit(self, chat_id: int, message_id: int, text: str, buttons: Buttons | None = None) -> None:
        try:
            await self.outbox.edit(chat_id, message_id, text, buttons)
        except Exception:
            log.warning("could not edit message %s", message_id, exc_info=True)

    # ---- orders ----

    async def _order(self, event: Message) -> str | None:
        arg = event.args.strip().lower()
        if arg not in ("", "mine", "all"):
            return "Usage: /order, /order mine or /order all"
        try:
            if not arg and orders.queue_has_many_owners(self.conn):  # same question as the chat path
                await orders.ask_scope(self.conn, self.outbox, event.chat_id, event.message_id, event.user_id)
            else:
                await orders.propose(self.conn, self.outbox, self.swiggy, self._address(), event.chat_id,
                                     event.user_id, arg or "all", event.message_id)
        except OrderError as e:
            return str(e)
        except SwiggyToolError:
            return "Swiggy refused the request. Try again in a bit."
        return None  # the card or the question is the reply

    async def _track(self, event: Message) -> str:
        arg = event.args.lstrip("#").strip()
        if arg and not arg.isdigit():
            return "Usage: /track or /track <order id>"
        return await orders.track(self.conn, self.swiggy, self._address(), int(arg) if arg else None)

    def _history(self, args: str) -> str:
        arg = args.lstrip("#").strip()
        if not arg:
            return orders.history_list(self.conn)
        if not arg.isdigit():
            return "Usage: /history or /history <order id>"
        try:
            return orders.history_detail(self.conn, int(arg))
        except OrderError as e:
            return str(e)

    def _resolve(self, event: Message) -> str:
        parts = event.args.lstrip("#").split()
        if len(parts) != 2 or not parts[0].isdigit() or parts[1].lower() not in ("placed", "failed"):
            return "Usage: /resolve <order id> placed|failed"
        try:
            text = orders.resolve(self.conn, int(parts[0]), parts[1].lower(), event.user_id)
        except OrderError as e:
            return str(e)
        ids = orders.trace_ids(self.conn, int(parts[0]))
        return text + (f"\nTraces: {', '.join(ids)}" if ids else "")

    def _list(self) -> str:
        rows = self.conn.execute(
            "SELECT l.id, l.slot, l.name, l.variant, l.qty, "
            "  (SELECT group_concat(m.display_name, ', ') FROM queue_line_owners o "
            "   JOIN members m ON m.user_id = o.user_id WHERE o.line_id = l.id) AS owners "
            "FROM queue_lines l WHERE l.status = 'queued' ORDER BY l.slot"
        ).fetchall()
        if not rows:
            return "Queue is empty."
        lines = ["Queue:"]
        for r in rows:
            lines.append(f"#{r['slot']} {queue.item_label(r['name'], r['variant'], r['qty'])} - {r['owners'] or 'nobody'}")
        return "\n".join(lines)

    async def _status(self) -> str:
        kv = dict(self.conn.execute("SELECT key, value FROM kv").fetchall())
        proposal, unknown = orders.open_proposal(self.conn), orders.unknown_order(self.conn)
        open_text = "no"
        if proposal:
            open_text = f"#{proposal['id']} ({proposal['scope']}), waiting for Confirm or Cancel"
        warn = (f"\nWARNING: order #{unknown['id']} is unknown. Check the Swiggy app, then "
                f"/resolve {unknown['id']} placed|failed") if unknown else ""
        members = self.conn.execute("SELECT count(*) FROM members").fetchone()[0]
        return (
            f"Swiggy login: {kv.get('auth_state')}\n"
            f"Swiggy address: {await self._address_text()}\n"
            f"Queue version: {kv.get('queue_version')}\n"
            f"Open order: {open_text}\n"
            f"Members: {members}\n"
            f"Group id: {self.group_id}" + warn
        )

    async def _address_text(self) -> str:
        current = self._address()
        if not current:
            return "not set"
        if not (self.swiggy and self.auth and self.auth.logged_in()):
            return "unknown (log in)"
        try:
            return await catalog.address_label(self.swiggy, current, self.conn) or "unknown"
        except (LoggedOut, SwiggyUnavailable):
            return "unknown (log in)"  # /status must not start a login or fail

    # ---- login ----

    def _pending_login(self) -> dict | None:
        """The one pending login as {id, payload}, or None. An expired one is deleted."""
        row = self.conn.execute(
            "SELECT id, payload_json, expires_at FROM pending WHERE kind='login'").fetchone()
        if not row:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            self.conn.execute("DELETE FROM pending WHERE id=?", (row["id"],))
            return None
        return {"id": row["id"], "payload": json.loads(row["payload_json"])}

    async def _login(self, event: Message) -> str | None:
        if not self.auth:
            return "Swiggy isn't set up."
        if self.auth.logged_in():
            return "Already logged in to Swiggy."
        login = self._pending_login() or await self._create_login(event.user_id)
        await self.outbox.send(event.chat_id, self._instructions(login["payload"]["url"]),
                               reply_to=event.message_id, html=True)
        return None

    async def _create_login(self, user_id: int) -> dict:
        url, state, verifier = await self.auth.start_login()
        payload = {"url": url, "state": state, "verifier": verifier, "parked": []}
        cur = self.conn.execute(
            "INSERT INTO pending (user_id, kind, payload_json, created_at, expires_at) "
            "VALUES (?, 'login', ?, ?, ?)",
            (user_id, json.dumps(payload), _now(), (datetime.now(timezone.utc) + LOGIN_TTL).isoformat()),
        )
        return {"id": cur.lastrowid, "payload": payload}

    @staticmethod
    def _instructions(url: str) -> str:
        """The login message as HTML (send it with html=True). The link sits in <code> so tapping it copies it."""
        e = html.escape
        return (e("Swiggy login:\n"
                  "1) Copy this link (tap it to copy) and open it in Safari or Chrome, not inside Telegram:\n")
                + f"<code>{e(url)}</code>\n"
                + e("2) Log in with OTP.\n"
                    "3) The last page won't load. That's expected. Copy its full address.\n"
                    "4) Reply to this message with that address within 2 minutes.\n"
                    "On a computer, just open the link there and do the same."))

    async def _login_ready(self, event: Message) -> tuple[dict, bool]:
        """The pending login, created if there is none. True when it is new (its link still has to be posted)."""
        login = self._pending_login()
        if login:
            return login, False
        return await self._create_login(event.user_id), True

    async def _logged_out_partway(self, event: Message, actions: list[str]) -> str:
        """Swiggy logged out after the agent changed the queue. Post the login link if none is pending,
        and say what was done; the person repeats the rest after logging in."""
        try:
            login, fresh = await self._login_ready(event)
            if fresh:
                await self.outbox.send(event.chat_id, self._instructions(login["payload"]["url"]), html=True)
        except SwiggyUnavailable:
            pass  # still tell them what was done; /login will retry the link
        return ("Swiggy logged out partway. I already did: " + "; ".join(actions)
                + ". After logging in, ask me again for the rest.")

    async def _park(self, event: Message) -> None:
        """Save the event until login finishes; post the login link only if none is pending."""
        login, fresh = await self._login_ready(event)
        login["payload"]["parked"].append(asdict(event))
        self.conn.execute("UPDATE pending SET payload_json=? WHERE id=?",
                          (json.dumps(login["payload"]), login["id"]))
        if fresh:
            await self.outbox.send(event.chat_id, self._instructions(login["payload"]["url"]), html=True)
        await self.outbox.send(event.chat_id, PARKED_REPLY, reply_to=event.message_id)

    async def _finish_login(self, event: Message, login: dict) -> None:
        p = login["payload"]
        try:
            await self.auth.finish_login(event.text, p["state"], p["verifier"])
        except LoginError as e:
            await self.outbox.send(event.chat_id, str(e), reply_to=event.message_id)
            return
        except SwiggyUnavailable:
            await self.outbox.send(event.chat_id, UNAVAILABLE, reply_to=event.message_id)
            return
        self.conn.execute("DELETE FROM pending WHERE id=?", (login["id"],))
        await self.outbox.send(event.chat_id, f"Logged in to Swiggy ✅ (by {event.display_name})",
                               reply_to=event.message_id)
        try:
            await self.outbox.delete(event.chat_id, event.message_id)
        except Exception:
            log.info("could not delete the pasted message (is the bot an admin?)")
        for parked in p["parked"]:
            self._replaying.add(parked["message_id"])
            await self.resubmit(Message(**parked))
