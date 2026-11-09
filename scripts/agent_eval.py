"""Manual eval of the agent against the REAL model. Not part of pytest; costs a few API calls.

    .venv/bin/python scripts/agent_eval.py

Uses OPENAI_API_KEY / AGENT_MODEL / AGENT_REASONING_EFFORT from .env, an in-memory database and a
fake Swiggy catalog. It prints the tool calls and the reply for each phrasing and asserts nothing:
read the output and judge it.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cartmates.agent.llm import LLMTurn, OpenAILLM  # noqa: E402
from cartmates.agent.loop import run_turn  # noqa: E402
from cartmates.config import load_config  # noqa: E402
from cartmates.domain import queue  # noqa: E402
from cartmates.store import db  # noqa: E402
from cartmates.telegram.events import Message  # noqa: E402
from cartmates.telegram.outbox import FakeOutbox  # noqa: E402


def variant(spin, size, price, stock=True, max_qty=5):
    return {"spinId": spin, "skuId": "K" + spin, "quantityDescription": size, "isInStockAndAvailable": stock,
            "maxQuantity": max_qty, "price": {"mrp": price + 2, "offerPrice": price}}


CATALOG = {
    "milk": [("Amul Taaza Toned Milk", [variant("m1", "500 ml", 29), variant("m2", "1 L", 56), variant("m3", "2 L", 108)]),
             ("Amul Gold Full Cream Milk", [variant("m4", "500 ml", 35), variant("m5", "1 L", 68)])],
    "maggi": [("Maggi 2-Minute Noodles", [variant("g1", "70 g", 14, max_qty=10), variant("g2", "280 g (4 pack)", 56)])],
    "egg": [("Farm Fresh Eggs", [variant("e1", "6 pcs", 66), variant("e2", "12 pcs", 125)])],
    "curd": [("Amul Masti Dahi", [variant("c1", "400 g", 35)])],
}


class FakeSwiggy:
    """Stands in for SwiggyClient: answers search_products / your_go_to_items from CATALOG."""

    async def call(self, tool, args):
        if tool == "your_go_to_items":
            found = CATALOG["milk"][:1] + CATALOG["egg"]
        else:
            words = [w.rstrip("s") for w in args["query"].lower().split() if len(w) > 2]
            found = [p for ps in CATALOG.values() for p in ps if any(w in p[0].lower() for w in words)]
        return {"products": [{"displayName": n, "brand": n.split()[0], "variations": v} for n, v in found]}


class LoggingLLM:
    """Wraps the real LLM and prints every round."""

    def __init__(self, inner):
        self.inner = inner

    async def respond(self, instructions, input_items, tools) -> LLMTurn:
        turn = await self.inner.respond(instructions, input_items, tools)
        for c in turn.tool_calls:
            print(f"    tool: {c.name}({c.arguments})")
        return turn


PHRASINGS = [
    "2 amul taaza 1L for me and rohan",
    "add maggi",
    "make the maggi 3",
    "drop the curd",
    "what's in the queue",
    "the usual milk",
    "eggs for everyone",
    "order now",
    "hi",
    "add 50 maggi",
]


def fresh_db() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.migrate(conn)
    for uid, name in ((1, "Priya"), (2, "Rohan"), (3, "Sneha")):
        conn.execute("INSERT INTO members (user_id, display_name, joined_at) VALUES (?, ?, 'x')", (uid, name))
    for name, size, price, uid in (("Maggi 2-Minute Noodles", "70 g", 1400, 1), ("Amul Masti Dahi", "400 g", 3500, 2)):
        with db.transaction(conn):
            queue.add_line(conn, {"spin": "s", "sku": "k", "name": name, "size": size, "price": price, "max_qty": 5},
                           1, [uid], name, uid)
    return conn


async def main() -> None:
    config = load_config()
    if not (config.openai_api_key and config.agent_model):
        sys.exit("Set OPENAI_API_KEY and AGENT_MODEL in .env first.")
    llm = LoggingLLM(OpenAILLM(config.openai_api_key, config.agent_model, config.agent_reasoning_effort))
    for n, text in enumerate(PHRASINGS, 1):
        conn, outbox = fresh_db(), FakeOutbox()  # each phrasing starts from the same small queue
        print(f'\n[{n}] Priya: "{text}"')
        event = Message(chat_id=-1, message_id=n, user_id=1, display_name="Priya", text=text)
        await run_turn(conn=conn, llm=llm, swiggy=FakeSwiggy(), address_id="addr", outbox=outbox,
                       event=event, text=text, free_delivery_min=config.free_delivery_min)
        for sent in outbox.sent:
            print(f"    reply: {sent.text}")
            for row in sent.buttons or []:
                print(f"    button: {row[0][0]}")
        for row in conn.execute("SELECT id, name, variant, qty FROM queue_lines WHERE status='queued'"):
            print(f"    queue: #{row['id']} {row['name']} {row['variant']} x{row['qty']} ->",
                  queue.owner_names(conn, row["id"]))


asyncio.run(main())
