"""Display-time tidying of Swiggy names. The database keeps Swiggy's full name; only views shorten it."""
import re

# A trailing "( ... )" group is dropped only when it lists claims ("a, b" or "a. b") or Swiggy cut it off.
# A single phrase such as "(Lactose Free)" says what the product is, so it stays.
_LIST = re.compile(r",\s*\S|\.\s+\S")
_TRIM = " -–,;:"
BUTTON_MAX = 60


def short_name(name: str) -> str:
    """'SuperYou Chips - Pudina (No Palm oil, Baked not fried)' -> 'SuperYou Chips - Pudina'."""
    s = name.split(" | ")[0]
    while True:
        i = s.rfind("(")
        tail = s[i:]
        closed = tail.rstrip().endswith(")")
        if i <= 0 or (closed and not _LIST.search(tail[1:-1])) or (not closed and ")" in tail):
            break
        s = s[:i].rstrip(_TRIM)
    return " ".join(s.split()).rstrip(_TRIM) or " ".join(name.split())


def _common_words(names: list[str]) -> int:
    """How many leading words every name shares."""
    split = [n.split() for n in names]
    n = 0
    while all(len(w) > n for w in split) and len({w[n] for w in split}) == 1:
        n += 1
    return n


def _fit(text: str, room: int) -> str:
    return text if len(text) <= room else text[:max(room - 1, 1)].rstrip() + "…"


def button_labels(options: list[dict], rupees) -> list[str]:
    """'<part that differs> · <size> · ₹<price>' for each option on one page. Only the name part is cut."""
    names = [short_name(o["name"]) for o in options]
    skip = _common_words(names) if len(names) > 1 else 0
    parts = [" ".join(n.split()[skip:]).lstrip(_TRIM) for n in names]
    if not all(parts):
        parts = names
    labels = []
    for part, o in zip(parts, options):
        tail = " · ".join(x for x in (o["size"], rupees(o["price"])) if x)
        labels.append(f"{_fit(part, BUTTON_MAX - len(tail) - 3)} · {tail}")
    return labels
