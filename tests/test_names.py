"""short_name and the button labels: table-driven, because the parsing has edge cases."""
import pytest

from queuify.domain.queue import rupees
from queuify.swiggy.names import button_labels, short_name

NAMES = [
    ("SuperYou 10g Protein Multigrain Chips - Pudina (No Palm oil, Baked not fried)",
     "SuperYou 10g Protein Multigrain Chips - Pudina"),
    ("SuperYou 10g Protein Wafer Bar - Strawberry Creme (Made with Atta & Jowar. No Added Sugar. No Trans Fat…",
     "SuperYou 10g Protein Wafer Bar - Strawberry Creme"),  # cut off by Swiggy, never closed
    ("NOICE High Protein Eggs (Nut & Bean Feed)", "NOICE High Protein Eggs (Nut & Bean Feed)"),
    ("NESCAFÉ Classic Instant Coffee | 100% Pure Coffee", "NESCAFÉ Classic Instant Coffee"),
    ("Tata Gold Chicory Mix With Strong Taste (Not Fried)", "Tata Gold Chicory Mix With Strong Taste (Not Fried)"),
    ("Tata Gold Chicory Mix With Strong Taste (Strong Taste, Not Fried, Rich)", "Tata Gold Chicory Mix With Strong Taste"),
    ("Amul Taaza Toned Milk (Lactose Free)", "Amul Taaza Toned Milk (Lactose Free)"),
    ("Coca-Cola Zero (Sugar Free)", "Coca-Cola Zero (Sugar Free)"),
    ("Aashirvaad Atta (Gluten Free)", "Aashirvaad Atta (Gluten Free)"),
    ("Epigamia Greek Yogurt (No Added Sugar)", "Epigamia Greek Yogurt (No Added Sugar)"),
    ("Lays Chips (Masala Munch)", "Lays Chips (Masala Munch)"),
    ("Amul Gold Full Cream Milk (4.5% Fat)", "Amul Gold Full Cream Milk (4.5% Fat)"),
    ("Fortune Oil (Pack of 1.5 L)", "Fortune Oil (Pack of 1.5 L)"),
    ("Lays Chips (Baked)", "Lays Chips (Baked)"),
    ("SuperYou Protein Chips | 13g protein | Multigrain, No Palm oil, Baked", "SuperYou Protein Chips"),
    ("Amul Taaza Toned Milk", "Amul Taaza Toned Milk"),
    ("Mother Dairy Curd (Cup) (Probiotic, No Added Sugar)", "Mother Dairy Curd (Cup)"),
    ("Yoga Bar Oats (NO PRESERVATIVES)", "Yoga Bar Oats (NO PRESERVATIVES)"),
    ("Brown Bread (Sliced) 400 g", "Brown Bread (Sliced) 400 g"),  # parentheses in the middle stay
    ("  Lays   Classic  Salted  ", "Lays Classic Salted"),
    ("(No Palm oil)", "(No Palm oil)"),  # never shortened to nothing
]


@pytest.mark.parametrize("raw,expected", NAMES)
def test_short_name(raw, expected):
    assert short_name(raw) == expected


def opt(name, size="40 g", price=3600):
    return {"name": name, "size": size, "price": price}


def test_labels_strip_the_shared_leading_words():
    a = opt("SuperYou 10g Protein Multigrain Chips - Cheese & Tomato (No Palm oil, Baked not fried)")
    b = opt("SuperYou 10g Protein Multigrain Chips - Super Masala (No Palm oil, Baked not fried)")
    assert button_labels([a, b], rupees) == ["Cheese & Tomato · 40 g · ₹36", "Super Masala · 40 g · ₹36"]


def test_a_single_option_keeps_its_full_short_name():
    a = opt("SuperYou Protein Chips - Cheese & Tomato (No Palm oil, Baked)")
    assert button_labels([a], rupees) == ["SuperYou Protein Chips - Cheese & Tomato · 40 g · ₹36"]


def test_same_product_in_two_sizes_is_not_stripped_to_nothing():
    a, b = opt("Amul Taaza", "500 ml", 3000), opt("Amul Taaza", "1 L", 6000)
    assert button_labels([a, b], rupees) == ["Amul Taaza · 500 ml · ₹30", "Amul Taaza · 1 L · ₹60"]


def test_a_very_long_name_is_cut_but_size_and_price_survive():
    a, b = opt("Brand " + "Long " * 30 + "One", "1.5 kg x 2", 123450), opt("Brand Other", "1 kg")
    long_label, _ = button_labels([a, b], rupees)
    assert len(long_label) <= 60 and long_label.endswith(" · 1.5 kg x 2 · ₹1234.50") and "…" in long_label
