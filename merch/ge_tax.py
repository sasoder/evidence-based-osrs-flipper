"""Grand Exchange tax helpers shared by execution accounting.

Flipping Utilities persists gross realized sell prices. Profit accounting must convert those
prices to net proceeds using the tax rules in effect when the sale occurred.
"""

from __future__ import annotations

GE_TAX_START = 1_639_072_800
GE_TAX_INCREASED = 1_748_514_600
GE_TAX_CAP = 5_000_000

# Stable item ids from Flipping Utilities' tax-exempt lists. Names are retained as a fallback
# for newer client mappings and old exports whose ids may be missing.
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


def tax_per_item(item_id: int, name: str, gross_price: int, timestamp_ms=None) -> int:
    if gross_price <= 0:
        return 0
    ts = _epoch_seconds(timestamp_ms)
    if ts is not None and ts < GE_TAX_START:
        return 0
    if item_id in TAX_EXEMPT_IDS or name.lower() in TAX_EXEMPT_NAMES:
        return 0
    rate = 0.01 if ts is not None and ts < GE_TAX_INCREASED else 0.02
    return min(int(gross_price * rate), GE_TAX_CAP)


def net_sale_price(item_id: int, name: str, gross_price: int, timestamp_ms=None) -> int:
    return gross_price - tax_per_item(item_id, name, gross_price, timestamp_ms)


def _epoch_seconds(value) -> float | None:
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e12 else float(value)
    return None
