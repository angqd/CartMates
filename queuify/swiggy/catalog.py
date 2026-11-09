import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from queuify.swiggy.client import SwiggyClient


@dataclass(frozen=True)
class Variant:
    spin_id: str
    sku_id: str
    size: str
    mrp_paise: int
    offer_paise: int
    in_stock: bool
    max_qty: int | None


@dataclass(frozen=True)
class Product:
    name: str
    brand: str
    variants: list[Variant]


def _paise(rupees) -> int:
    # Via str so 116.1 does not become 11609.999...
    return int(Decimal(str(rupees)) * 100)


def _merge(products: list[Product]) -> list[Product]:
    """Swiggy sometimes lists one product twice: fold products that share a spin id into the first."""
    merged: list[Product] = []
    for p in products:
        spins = {v.spin_id for v in p.variants}
        same = next((m for m in merged if spins & {v.spin_id for v in m.variants}), None)
        if not same:
            merged.append(p)
            continue
        have = {v.spin_id for v in same.variants}
        same.variants.extend(v for v in p.variants if v.spin_id not in have)
    return merged


def _products(data: dict) -> list[Product]:
    return _merge([
        Product(
            name=p["displayName"],
            brand=p.get("brand", ""),
            variants=[
                Variant(
                    spin_id=v["spinId"], sku_id=v["skuId"], size=v.get("quantityDescription", ""),
                    mrp_paise=_paise(v["price"]["mrp"]), offer_paise=_paise(v["price"]["offerPrice"]),
                    in_stock=bool(v.get("isInStockAndAvailable")), max_qty=v.get("maxQuantity"),
                )
                for v in p.get("variations", [])
            ],
        )
        for p in data.get("products", [])
    ])


async def search(client: SwiggyClient, address_id: str, query: str) -> list[Product]:
    return _products(await client.call("search_products", {"addressId": address_id, "query": query}))


async def go_to_items(client: SwiggyClient, address_id: str) -> list[Product]:
    """The household's frequently bought items; the same shape as a search."""
    return _products(await client.call("your_go_to_items", {"addressId": address_id}))


def _same_address(a: str, b: str) -> bool:
    # Responses sometimes give only the part before "__".
    return a.split("__")[0] == b.split("__")[0]


async def list_addresses(client: SwiggyClient) -> list[dict]:
    """Every saved address, all pages."""
    found: list[dict] = []
    page = 1
    while True:
        data = await client.call("get_addresses", {"page": page})
        found += data.get("addresses", [])
        if not data.get("pagination", {}).get("hasMore"):
            return found
        page += 1


async def address_label(client: SwiggyClient, address_id: str,
                        conn: sqlite3.Connection | None = None) -> str | None:
    """The saved address's tag (e.g. "Home"), cached in kv so /status stays cheap."""
    if conn:
        row = conn.execute("SELECT value FROM kv WHERE key='address_label'").fetchone()
        cached = json.loads(row[0]) if row else None
        if cached and _same_address(cached["id"], address_id):
            return cached["label"]
    page = 1
    while True:
        data = await client.call("get_addresses", {"page": page})
        for a in data.get("addresses", []):
            if _same_address(a["id"], address_id):
                label = a.get("addressTag") or a.get("addressCategory")
                if conn and label:
                    conn.execute("INSERT INTO kv (key, value) VALUES ('address_label', ?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (json.dumps({"id": address_id, "label": label}),))
                return label
        if not data.get("pagination", {}).get("hasMore"):
            return None
        page += 1
