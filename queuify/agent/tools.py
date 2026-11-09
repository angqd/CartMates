"""The tools the model can call. Each is a JSON schema plus a function over the domain code.
Model output is untrusted, so every argument is checked here."""
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from queuify import tracing
from queuify.domain import orders, pending, queue
from queuify.domain.queue import QueueError, rupees
from queuify.store import db
from queuify.swiggy import catalog
from queuify.swiggy.names import short_name
from queuify.swiggy.errors import SwiggyToolError, SwiggyUnavailable
from queuify.telegram.outbox import Mention, Outbox

log = logging.getLogger(__name__)

SHOW_PRODUCTS, SHOW_VARIANTS = 20, 6
MAX_CANDIDATES = 24  # picks plus the "More options" extras


@dataclass(frozen=True)
class Ref:
    """One variant the model saw in a search this turn."""
    name: str
    size: str
    spin: str
    sku: str
    price: int  # paise
    in_stock: bool
    max_qty: int | None
    search: int = 0  # which search of this turn it came from

    def option(self) -> dict:  # the shape stored in a pending row and used by queue.add_line
        return {"spin": self.spin, "sku": self.sku, "name": self.name, "size": self.size,
                "price": self.price, "max_qty": self.max_qty}


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "name": name, "description": description, "strict": False,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}


_OWNERS = {"type": "array", "items": {"type": "string"},
           "description": 'Member names, or "me" for the sender. Omit for just the sender.'}
_QTY = {"type": "integer", "description": "How many units (1-20)."}
_NUMBER = {"type": "integer", "description": "The queue number shown as #n."}
_TEXT = {"type": "string", "description": "The person's own words for this item."}

SCHEMAS = [
    _fn("search_catalog", "Search Swiggy Instamart at the household address. Returns every product Swiggy lists "
        "(up to 20), one line each with its variants, each variant with a ref like p1v2. Flavours and "
        "types are separate products; sizes are variants of one product.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("go_to_items", "The household's frequently bought items ('the usual'), same format as a search.",
        {}, []),
    _fn("add_to_queue", "Add one variant from a search made in THIS turn to the queue.",
        {"ref": {"type": "string", "description": "A ref such as p1v2 from search_catalog or go_to_items."},
         "qty": _QTY, "owners": _OWNERS, "original_text": _TEXT}, ["ref", "qty", "original_text"]),
    _fn("ask_variant", "Ask the sender to pick between 2-4 variants with buttons (the bot adds the other in-stock variants of that search behind a More button and writes the labels). Does not end your turn: "
        "call it once for each ambiguous item, keep going with the other items, then write one short summary.",
        {"question": {"type": "string", "description": "A short question shown above the buttons."},
         "refs": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4},
         "qty": _QTY, "owners": _OWNERS, "original_text": _TEXT}, ["question", "refs", "qty", "original_text"]),
    _fn("close_question", "Use after the sender answers their own open question in text (e.g. 'the pudina one'): "
        "add the item with add_to_queue, then close the question so its buttons can't add it again. "
        "Only the person who was asked can close a question.",
        {"question_id": {"type": "integer", "description": "The #id of the question in the snapshot's Open questions."}},
        ["question_id"]),
    _fn("propose_order", "Start an order. scope 'all' = the whole queue, 'mine' = only the sender's items, "
        "'ask' = let the sender choose with buttons. The bot posts the bill card or the question itself. "
        "Ends your turn: write no reply after calling it.",
        {"scope": {"type": "string", "enum": ["all", "mine", "ask"]}}, ["scope"]),
    _fn("update_queue_item", "Change the quantity and/or owners of a queued line, by its queue number.",
        {"number": _NUMBER, "qty": _QTY, "owners": _OWNERS}, ["number"]),
    _fn("remove_from_queue", "Remove a queued line by its queue number.", {"number": _NUMBER}, ["number"]),
    _fn("list_queue", "The current queue lines with their numbers, owners and the total.", {}, []),
    _fn("track_order", "Live delivery status (ETA) of a placed order. Read-only. Defaults to the most recent "
        "placed order. Use for 'where is my order'.",
        {"order_id": {"type": "integer", "description": "The bot's order #id. Omit for the latest placed order."}}, []),
    _fn("order_history", "Recent orders (id, date, who ordered, total, status), newest first. Read-only. "
        "Use for 'what did we order last week'.",
        {"count": {"type": "integer", "description": "How many orders (1-10, default 5)."}}, []),
]


class Toolbox:
    """One per agent turn: it holds the turn's registry of searched variants."""

    def __init__(self, conn: sqlite3.Connection, swiggy, address_id: str | None, outbox: Outbox,
                 chat_id: int, reply_to: int, user_id: int, user_name: str, now: datetime):
        self.conn, self.swiggy, self.address_id, self.outbox = conn, swiggy, address_id, outbox
        self.chat_id, self.reply_to, self.user_id, self.user_name, self.now = chat_id, reply_to, user_id, user_name, now
        self.registry: dict[str, Ref] = {}
        self.actions: list[str] = []  # one-line summaries of successful changes, for chat_memory
        self.ended: str | None = None  # set when a tool posts its own message (an order card or scope question): the loop then stops, with no model reply
        self.ended_message_id: int | None = None  # the Telegram id of the message that ended the turn
        self.outcome = "replied"  # "proposed" once an order card / scope question ended the turn
        self.told: dict[int, list[str]] = {}  # owner id -> what someone else did to their lines this turn
        self.asked = 0  # variant questions sent this turn
        self._products = 0
        self.searches: list[list[Ref]] = []  # every ref of each search this turn, in Swiggy's order
        self._tools = {"search_catalog": self.search_catalog, "go_to_items": self.go_to_items,
                       "add_to_queue": self.add_to_queue, "ask_variant": self.ask_variant,
                       "close_question": self.close_question,
                       "propose_order": self.propose_order,
                       "update_queue_item": self.update_queue_item,
                       "remove_from_queue": self.remove_from_queue, "list_queue": self.list_queue,
                       "track_order": self.track_order, "order_history": self.order_history}

    def mentions(self) -> list[Mention]:
        """One mention per affected owner, naming them so they are notified. Decided here, not by the model."""
        return [Mention(uid, queue.name_of(self.conn, uid), "; ".join(notes)) for uid, notes in self.told.items()]

    def _tell_owners(self, owners: list[int], what: str) -> None:
        for uid in owners:
            self.told.setdefault(uid, []).append(f"{self.user_name} {what}")

    async def run(self, name: str, raw_arguments: str) -> str:
        """One call inside a "tool:<name>" span."""
        with tracing.span(f"tool:{name}", tracing.TOOL, {"tool.name": name}) as sp:
            tracing.set_text(sp, "tool.parameters", raw_arguments)
            result = await self._run(name, raw_arguments)
            tracing.set_text(sp, "output.value", result)
            if result.startswith("error:"):
                sp.set_attribute("queuify.tool_error", True)
            return result

    async def _run(self, name: str, raw_arguments: str) -> str:
        """Execute one call. Rule violations come back as text for the model; never raised.
        LoggedOut is deliberately not caught: the handler parks the whole message."""
        tool = self._tools.get(name)
        if not tool:
            return f"error: no tool named {name}"
        try:
            args = json.loads(raw_arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            return "error: arguments were not a JSON object"
        queue.log_event(self.conn, self.user_id, "tool_call", {"name": name, "args": args})
        try:
            return await tool(args)
        except QueueError as e:
            return f"error: {e}"
        except (SwiggyUnavailable, SwiggyToolError):
            return "error: Swiggy isn't reachable right now; tell the user to try again in a bit"

    # ---- helpers ----

    def _qty(self, args: dict) -> int:
        qty = args.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, int):
            raise QueueError("qty must be a whole number")
        return qty

    def _owners(self, args: dict) -> list[int]:
        names = args.get("owners") or []
        if not isinstance(names, list):
            raise QueueError("owners must be a list of names")
        return queue.resolve_owners(self.conn, names, self.user_id)

    def _ref(self, args: dict) -> Ref:
        ref = self.registry.get(str(args.get("ref") or ""))
        if not ref:
            raise QueueError("unknown ref. Call search_catalog (or go_to_items) first and use a ref it returned this turn")
        if not ref.in_stock:
            raise QueueError(f"{ref.name} {ref.size} is out of stock")
        return ref

    def _show(self, products: list[catalog.Product], query: str = "") -> str:
        """One line per product: 'p3 Bru Instant Coffee Pouch: v1 50 g ₹119 · v2 50 g ×2 ₹238 OUT'."""
        said = f' for "{query}"' if query else ""
        if not products:
            return f"No results{said}."
        found: list[Ref] = []
        self.searches.append(found)
        lines = []
        for product in products[:SHOW_PRODUCTS]:
            self._products += 1
            parts = []
            for n, v in enumerate(product.variants[:SHOW_VARIANTS], 1):
                ref = Ref(product.name, v.size, v.spin_id, v.sku_id, v.offer_paise, v.in_stock, v.max_qty,
                          len(self.searches) - 1)
                self.registry[f"p{self._products}v{n}"] = ref
                found.append(ref)
                parts.append(f"v{n} {v.size} {rupees(v.offer_paise)}" + ("" if v.in_stock else " OUT"))
            lines.append(f"p{self._products} {short_name(product.name)}: " + " · ".join(parts))
        if query:  # the facts to explain "why wasn't X shown"
            lines.append(f'(search "{query}": {len(products)} products returned, {len(lines)} shown)')
        return "\n".join(lines)

    def _candidates(self, picks: list[Ref]) -> list[dict]:
        """The agent's picks, then the other in-stock variants of the first pick's search, no repeats."""
        extras = self.searches[picks[0].search] if picks else []
        out: list[dict] = []
        for ref in [*picks, *(r for r in extras if r.in_stock)]:
            option = ref.option()
            if all((o["spin"], o["sku"]) != (option["spin"], option["sku"]) for o in out):
                out.append(option)
        return out[:MAX_CANDIDATES]

    def _address(self) -> str:
        if not (self.swiggy and self.address_id):
            raise QueueError("no Swiggy address is pinned, so the catalog can't be searched")
        return self.address_id

    # ---- tools ----

    async def search_catalog(self, args: dict) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise QueueError("query is empty")
        return self._show(await catalog.search(self.swiggy, self._address(), query), query)

    async def go_to_items(self, args: dict) -> str:
        return self._show(await catalog.go_to_items(self.swiggy, self._address()))

    async def add_to_queue(self, args: dict) -> str:
        ref, qty, owners = self._ref(args), self._qty(args), self._owners(args)
        with db.transaction(self.conn):
            slot, text = queue.add_line(self.conn, ref.option(), qty, owners,
                                           str(args.get("original_text") or ""), self.user_id)
        self.actions.append(f"added #{slot} {text}")
        return f"Added #{slot} {text}. Queue total now {rupees(queue.queued_total_paise(self.conn))}."

    async def ask_variant(self, args: dict) -> str:
        refs = args.get("refs")
        if not isinstance(refs, list) or not 2 <= len(set(map(str, refs))) <= 4:
            raise QueueError("refs must be 2-4 different refs")
        picks = [self._ref({"ref": r}) for r in dict.fromkeys(map(str, refs))]
        options = self._candidates(picks)
        qty, owners = self._qty(args), self._owners(args)
        if not 1 <= qty <= queue.MAX_QTY:
            raise QueueError(f"qty must be between 1 and {queue.MAX_QTY}")
        question = str(args.get("question") or "Which one?")
        pending_id = pending.create_variant(self.conn, self.user_id, {
            "question": question, "options": options, "picked": len(picks), "qty": qty, "owners": owners,
            "original_text": str(args.get("original_text") or "")})
        try:
            sent_id = await self.outbox.send(self.chat_id, question, reply_to=self.reply_to,
                                             buttons=pending.variant_buttons(pending_id, options))
        except Exception:  # nobody can answer a question that never posted: don't leave its row behind
            pending.delete(self.conn, pending_id)
            raise
        pending.set_message_id(self.conn, pending_id, sent_id)
        self.asked += 1
        self.actions.append(f'asked {self.user_name} "{question}" (question #{pending_id})')
        return f"Question #{pending_id} sent; keep going with the other items, then write one short summary reply."

    async def close_question(self, args: dict) -> str:
        question_id = args.get("question_id")
        if isinstance(question_id, bool) or not isinstance(question_id, int):
            raise QueueError("question_id must be a whole number")
        row = pending.get_variant(self.conn, question_id)
        if not row:
            raise QueueError(f"there is no open question #{question_id}")
        if row["user_id"] != self.user_id:
            raise QueueError(f"only {queue.name_of(self.conn, row['user_id'])} can close that")
        message_id = self.conn.execute("SELECT message_id FROM pending WHERE id=?", (question_id,)).fetchone()[0]
        pending.delete(self.conn, question_id)
        text = row["payload"]["question"]
        if message_id:
            try:
                await self.outbox.edit(self.chat_id, message_id, "Answered in chat")  # no buttons
            except Exception:  # the question is closed either way; a failed edit only leaves stale buttons
                log.warning("could not edit question #%d's message", question_id, exc_info=True)
        self.actions.append(f'{self.user_name} answered "{text}" in chat (question #{question_id} closed)')
        return f"Closed question #{question_id}."

    async def propose_order(self, args: dict) -> str:
        scope = args.get("scope")
        if scope not in ("all", "mine", "ask"):
            raise QueueError("scope must be all, mine or ask")
        if scope == "ask":
            self.ended = await orders.ask_scope(self.conn, self.outbox, self.chat_id, self.reply_to, self.user_id)
        else:
            order_id = await orders.propose(self.conn, self.outbox, self.swiggy, self.address_id, self.chat_id,
                                            self.user_id, scope, self.reply_to)
            self.ended = f"(posted the bill card for order #{order_id})"
        self.outcome = "proposed"
        return "Card sent; the turn ends here."

    async def update_queue_item(self, args: dict) -> str:
        number = args.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            raise QueueError("number must be a whole number")
        qty = self._qty(args) if args.get("qty") is not None else None
        owners = self._owners(args) if args.get("owners") else None
        with db.transaction(self.conn):
            others, item = queue.other_owners(self.conn, number, self.user_id)
            text = queue.update_line(self.conn, number, self.user_id, qty, owners)
        self._tell_owners(others, f"changed your {item} (#{number})")
        self.actions.append(f"{self.user_name}: {text}")
        return text

    async def remove_from_queue(self, args: dict) -> str:
        number = args.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            raise QueueError("number must be a whole number")
        with db.transaction(self.conn):
            others, item = queue.other_owners(self.conn, number, self.user_id)
            text = queue.remove_line(self.conn, number, self.user_id)
        self._tell_owners(others, f"removed your {item} (#{number})")
        self.actions.append(f"{self.user_name}: {text}")
        return text

    async def list_queue(self, args: dict) -> str:
        lines = queue.queued_lines(self.conn)
        if not lines:
            return "Queue is empty."
        rows = [f"#{l['slot']} {queue.item_label(l['name'], l['variant'], l['qty'])} for "
                f"{queue.owner_names(self.conn, l['id'])}" for l in lines]
        return "\n".join(rows) + f"\nTotal {rupees(queue.queued_total_paise(self.conn))}"

    async def track_order(self, args: dict) -> str:
        order_id = args.get("order_id")
        if order_id is not None and (isinstance(order_id, bool) or not isinstance(order_id, int)):
            raise QueueError("order_id must be a whole number")
        return await orders.track(self.conn, self.swiggy, self.address_id, order_id)

    async def order_history(self, args: dict) -> str:
        count = args.get("count", 5)
        if isinstance(count, bool) or not isinstance(count, int):
            raise QueueError("count must be a whole number")
        return orders.history_list(self.conn, max(1, min(count, orders.HISTORY_LIMIT)))
