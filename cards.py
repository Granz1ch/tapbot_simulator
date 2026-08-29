"""10 cards: 5 common (always droppable), 5 rare (need X5 LUCK)."""

from dataclasses import dataclass
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets" / "cards"


@dataclass(frozen=True)
class Card:
    id: str
    name: str
    emoji: str
    rarity: str  # common | rare
    description: str


CARDS: dict[str, Card] = {
    "wooden_tap": Card("wooden_tap", "Wooden Tap", "🪵", "common", "A simple wooden tap. Everyday grind."),
    "copper_tap": Card("copper_tap", "Copper Tap", "🟤", "common", "Warm copper, slightly better clicks."),
    "steel_tap": Card("steel_tap", "Steel Tap", "⚙️", "common", "Industrial steel. Reliable."),
    "golden_tap": Card("golden_tap", "Golden Tap", "🥇", "common", "Shiny gold. Feels lucky already."),
    "crystal_tap": Card("crystal_tap", "Crystal Tap", "💎", "common", "Clear crystal. The last common prize."),
    "neon_tap": Card("neon_tap", "Neon Tap", "💜", "rare", "Glows in the dark. Needs extra luck."),
    "void_tap": Card("void_tap", "Void Tap", "🕳️", "rare", "Pulls coins from nowhere."),
    "phoenix_tap": Card("phoenix_tap", "Phoenix Tap", "🔥", "rare", "Burns and respawns every spin."),
    "aurora_tap": Card("aurora_tap", "Aurora Tap", "🌌", "rare", "Northern lights in your inventory."),
    "legend_tap": Card("legend_tap", "Legend Tap", "👑", "rare", "The mythical tenth card."),
    "titan_tap": Card(
        "titan_tap",
        "Titan Tap",
        "🌑",
        "exclusive",
        "The exclusive sixth card. Never drops from rolls — only from a Rare boss, for one hitter.",
    ),
    "crown_tap": Card(
        "crown_tap",
        "Crown Tap",
        "⚜️",
        "exclusive",
        "The TOP-1 card. Serial number and nickname are permanent. Holding it grants x2 coins and x2 luck. One per player.",
    ),
    "shield_tap": Card(
        "shield_tap",
        "Shield Tap",
        "🛡️",
        "exclusive",
        "Immunity during Brawl: cannot attack and cannot be targeted. Neutral.",
    ),
}

COMMON_IDS = [c.id for c in CARDS.values() if c.rarity == "common"]
RARE_IDS = [c.id for c in CARDS.values() if c.rarity == "rare"]
EXCLUSIVE_IDS = [c.id for c in CARDS.values() if c.rarity == "exclusive"]
BOSS_CARD_ID = "titan_tap"
CROWN_CARD_ID = "crown_tap"
SHIELD_CARD_ID = "shield_tap"
UNIQUE_IDS = {CROWN_CARD_ID}

# Higher = better. Craft result must beat the ingredients together.
CARD_POWER: dict[str, int] = {
    "wooden_tap": 1,
    "copper_tap": 2,
    "steel_tap": 3,
    "golden_tap": 4,
    "crystal_tap": 5,
    "neon_tap": 8,
    "void_tap": 9,
    "phoenix_tap": 10,
    "aurora_tap": 11,
    "legend_tap": 12,
    "titan_tap": 18,
    "crown_tap": 22,
    "shield_tap": 16,
}


def card_power(card_id: str) -> int:
    return int(CARD_POWER.get(card_id) or 0)

# Only first five (common) can be sold. Cap 50.
SELL_PRICES: dict[str, int] = {
    "wooden_tap": 10,
    "copper_tap": 18,
    "steel_tap": 28,
    "golden_tap": 40,
    "crystal_tap": 50,
}
MAX_SELL_PRICE = 50


def sell_price(card_id: str) -> int | None:
    if card_id not in SELL_PRICES:
        return None
    return min(SELL_PRICES[card_id], MAX_SELL_PRICE)


def resolve_card(query: str) -> Card | None:
    q = query.strip().lower().replace(" ", "_").replace("-", "_")
    if q in CARDS:
        return CARDS[q]
    for card in CARDS.values():
        if card.name.lower().replace(" ", "_") == q:
            return card
        if q in card.name.lower() or q in card.id:
            return card
    return None


def format_card(card: Card) -> str:
    badge = {"common": "COMMON", "rare": "RARE", "exclusive": "EXCLUSIVE"}.get(card.rarity, card.rarity.upper())
    return f"{card.emoji} **{card.name}** `{badge}` — {card.description}"


def card_image(card_id: str) -> Path | None:
    p = ASSETS / f"{card_id}.jpg"
    return p if p.is_file() else None
