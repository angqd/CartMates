"""Cart quotes, the COD check, checkout and recent orders on top of SwiggyClient.
Money is parsed into integer paise. update_cart overwrites the host's own Instamart cart (accepted)."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from cartmates.swiggy.client import SwiggyClient


class CartError(Exception):
    """The cart answer could not be trusted (wrong address, no total). The message is fit to show."""


def parse_paise(value) -> int:
    """'₹327', '₹12.60', 'FREE', 314, 12.6 -> paise. Via str so floats don't drift."""
    if value is None:
        return 0
    if isinstance(value, str):
        text = value.replace("₹", "").replace(",", "").replace("−", "-").strip()
        if text == "" or text.upper() == "FREE":
            return 0
    else:
        text = str(value)
    try:
        return int((Decimal(text) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        raise CartError(f"could not read the amount {value!r}") from None


def same_address(a: str, b: str) -> bool:
    # Responses sometimes give only the part before "__".
    return a.split("__")[0] == b.split("__")[0]


@dataclass(frozen=True)
class SpinQuote:
    spin_id: str
    requested: int
    in_cart: int          # quantity Swiggy actually put in the cart (0 if missing)
    in_stock: bool
    unit_paise: int       # price of one unit
    line_paise: int       # what the cart charges for in_cart units

    @property
    def available(self) -> bool:
        return self.in_stock and self.in_cart >= self.requested


@dataclass(frozen=True)
class Quote:
    spins: dict[str, SpinQuote]
    bill_lines: list[tuple[str, str]]   # Swiggy's own labels and texts, in order
    item_total_paise: int
    to_pay_paise: int
    price_basis: str                    # "unit", "line", or "unit?" when neither reading matched
    unavailable: list[str] = field(default_factory=list)  # spin ids


def merge_lines(lines) -> dict[str, tuple[str, int]]:
    """(spin_id, sku_id, qty) tuples -> {spin_id: (sku_id, summed qty)}."""
    merged: dict[str, tuple[str, int]] = {}
    for spin_id, sku_id, qty in lines:
        old = merged.get(spin_id)
        merged[spin_id] = (old[0] if old else sku_id, (old[1] if old else 0) + qty)
    return merged


def _bill(data: dict) -> tuple[list[tuple[str, str]], int | None, int]:
    bill = data.get("billBreakdown") or {}
    lines = [(str(i.get("label", "")), str(i.get("value", ""))) for i in bill.get("lineItems", [])]
    item_total = next((parse_paise(v) for k, v in lines if k.casefold().startswith("item total")), None)
    to_pay = (bill.get("toPay") or {}).get("value")
    if to_pay is None:
        to_pay = data.get("cartTotalAmount")
    if to_pay is None:
        raise CartError("Swiggy's cart had no total")
    return lines, item_total, parse_paise(to_pay)


def _price_basis(rows: list[tuple[dict, int, int]], item_total: int | None) -> str:
    """Is discountedFinalPrice a unit price or a line price? Whichever reading adds up to Item Total.
    rows are (cart item, price paise, cart qty). Ambiguous (e.g. every qty is 1) counts as unit."""
    if item_total is None:
        return "unit?"
    as_unit = sum(price * qty for _, price, qty in rows)
    as_line = sum(price for _, price, _ in rows)
    if as_unit == item_total:
        return "unit"
    return "line" if as_line == item_total else "unit?"


def read_quote(data: dict, address_id: str, merged: dict[str, tuple[str, int]]) -> Quote:
    selected = data.get("selectedAddress")
    if not selected or not same_address(str(selected), address_id):
        raise CartError("Swiggy's cart is on a different address than the pinned one; not going on.")
    bill_lines, item_total, to_pay = _bill(data)
    # Swiggy adds its own Rs 0 promo items (flyers); only spins we sent count.
    ours = {i["spinId"]: i for i in data.get("items", []) if i.get("spinId") in merged}
    rows = [(i, parse_paise(i.get("discountedFinalPrice")), int(i.get("quantity") or 0)) for i in ours.values()]
    basis = _price_basis(rows, item_total)
    spins = {}
    for spin_id, (_, requested) in merged.items():
        item = ours.get(spin_id)
        if not item:
            spins[spin_id] = SpinQuote(spin_id, requested, 0, False, 0, 0)
            continue
        price, qty = parse_paise(item.get("discountedFinalPrice")), int(item.get("quantity") or 0)
        if basis == "line":
            unit, line = (price // qty if qty else 0), price
        else:
            unit, line = price, price * qty
        spins[spin_id] = SpinQuote(spin_id, requested, qty, bool(item.get("isInStockAndAvailable")), unit, line)
    unavailable = [s for s, q in spins.items() if not q.available]
    return Quote(spins, bill_lines, item_total if item_total is not None else sum(q.line_paise for q in spins.values()),
                 to_pay, basis, unavailable)


async def quote(client: SwiggyClient, address_id: str, lines) -> Quote:
    """One update_cart for the lines (merged per spin_id). `lines` are (spin_id, sku_id, qty)."""
    merged = merge_lines(lines)
    items = [{"spinId": spin, "skuId": sku, "quantity": qty} for spin, (sku, qty) in merged.items()]
    data = await client.call("update_cart", {"selectedAddressId": address_id, "items": items})
    return read_quote(data, address_id, merged)


async def cod_available(client: SwiggyClient) -> bool:
    data = await client.call("get_payment_options", {})
    return (data.get("cod") or {}).get("available") is True


async def checkout_cod(client: SwiggyClient, address_id: str) -> dict:
    """Never retried on a 429: a retry could place the order twice."""
    return await client.call("checkout", {"addressId": address_id, "paymentMethod": "Cash"},
                             retry_rate_limit=False)


async def checkout_upi(client: SwiggyClient, address_id: str, app: str) -> dict:
    """Never retried on a 429, like COD: a retry could start a second payment."""
    return await client.call("checkout", {"addressId": address_id, "paymentMethod": "UPI", "intentApp": app},
                             retry_rate_limit=False)


async def payment_status(client: SwiggyClient, paas_id: str, order_id: str) -> dict:
    return await client.call("check_payment_status", {"paasId": paas_id, "orderId": order_id})


async def confirm_order(client: SwiggyClient, order_id: str, paas_id: str) -> dict:
    return await client.call("confirm_order", {"orderId": order_id, "paasId": paas_id})


async def delivery_status(client: SwiggyClient, order_id: str, address_id: str) -> dict:
    return await client.call("get_delivery_status", {"orderId": order_id, "addressId": address_id})


def status_of(result) -> str:
    """The answer's status, lower-cased ('' if missing)."""
    return str(result.get("status") or "").casefold() if isinstance(result, dict) else ""


def confirmed_order_id(result: dict) -> str | None:
    """The Swiggy order id if (and only if) the checkout answer says CONFIRMED."""
    if isinstance(result, dict) and status_of(result) == "confirmed" and result.get("orderId"):
        return str(result["orderId"])
    return None


@dataclass(frozen=True)
class RecentOrder:
    order_id: str
    created_at: datetime
    address_id: str
    item_names: list[str]
    status: str = ""          # Swiggy's status, e.g. DELIVERED
    payment_status: str = ""  # e.g. SUCCESS
    total_paise: int | None = None  # billDetails.grandTotal (or totalAmount); None when Swiggy left it out
    payment_method: str = ""  # e.g. UPIIntent

    @property
    def is_upi(self) -> bool:
        return "UPI" in self.payment_method.upper()

    @property
    def live(self) -> bool:
        """False for an order that failed, was cancelled or was never paid: it doesn't count as placed."""
        text = f"{self.status} {self.payment_status}".upper()
        return not any(bad in text for bad in ("FAIL", "CANCEL", "PENDING"))


async def recent_orders(client: SwiggyClient, count: int = 5) -> list[RecentOrder]:
    data = await client.call("get_orders", {"count": count})
    out = []
    for o in data.get("orders", []):
        try:
            created = datetime.fromisoformat(str(o["createdAt"]).replace("Z", "+00:00"))
            total = (o.get("billDetails") or {}).get("grandTotal", o.get("totalAmount"))
            out.append(RecentOrder(str(o["orderId"]), created, str((o.get("deliveryAddress") or {}).get("id") or ""),
                                   [str(i.get("name", "")) for i in o.get("items", [])],
                                   str(o.get("status") or ""), str(o.get("paymentStatus") or ""),
                                   parse_paise(total) if total is not None else None,
                                   str(o.get("paymentMethod") or "")))
        except (KeyError, ValueError, CartError):
            continue  # an order we can't read can't match
    return out
