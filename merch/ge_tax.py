"""Grand Exchange tax helpers shared by execution accounting.

Flipping Utilities persists gross realized sell prices. Profit accounting must convert those
prices to net proceeds. Current rule: 2% on sales, rounded down, capped at 5M — plus Jagex's
exempt-item list for tools, food, teleports, and ammo.
"""

from __future__ import annotations

GE_TAX_CAP = 5_000_000

TAX_EXEMPT_IDS = {
    233, 952, 1733, 1735, 1755, 2347, 5325, 5329, 5331, 5341, 5343, 8794, 13190,
}
TAX_EXEMPT_NAMES = {
    "ardougne teleport", "bass", "bread", "bronze arrow", "bronze dart", "cake",
    "camelot teleport", "chisel", "cooked chicken", "cooked meat", "energy potion(1)",
    "energy potion(2)", "energy potion(3)", "energy potion(4)", "falador teleport",
    "fortis teleport", "gardening trowel", "hammer", "herring", "iron arrow", "iron dart",
    "kourend castle teleport", "lobster", "lumbridge teleport", "mackerel", "meat pie",
    "mind rune", "minigame teleport", "needle", "old school bond", "pestle and mortar",
    "pike", "rake", "ring of dueling(8)", "salmon", "saw", "secateurs", "seed dibber",
    "shears", "shrimp", "spade", "steel arrow", "steel dart", "teleport to house", "tuna",
    "varrock teleport", "watering can(0)",
}


def sale_tax(gross_price: int) -> int:
    if gross_price <= 0:
        return 0
    return min(int(gross_price * 0.02), GE_TAX_CAP)


def tax_per_item(item_id: int, name: str, gross_price: int) -> int:
    if gross_price <= 0:
        return 0
    if item_id in TAX_EXEMPT_IDS or name.lower() in TAX_EXEMPT_NAMES:
        return 0
    return sale_tax(gross_price)


def net_sale_price(item_id: int, name: str, gross_price: int) -> int:
    return gross_price - tax_per_item(item_id, name, gross_price)
