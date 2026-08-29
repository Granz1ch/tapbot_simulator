"""Rotating card crafts. 4 shared recipes / 30 min + admin custom."""

from __future__ import annotations

import random
import time

import discord

import database as db
import psx_shop
from cards import (
    CARDS,
    COMMON_IDS,
    EXCLUSIVE_IDS,
    RARE_IDS,
    UNIQUE_IDS,
    card_power,
    format_card,
    resolve_card,
)

WINDOW = 30 * 60
CRAFT_POOL = list(COMMON_IDS) + list(RARE_IDS)


def window_id(now: float | None = None) -> int:
    return int((now or time.time()) // WINDOW)


def window_end(now: float | None = None) -> int:
    return (window_id(now) + 1) * WINDOW


def combined_floor(ings: list[str]) -> int:
    """Result must match the ingredients together: at least the best, and at least half the sum."""
    ps = [card_power(i) for i in ings]
    if not ps:
        return 0
    return max(max(ps), (sum(ps) + 1) // 2)


def recipe_legal(ings: list[str], result: str) -> tuple[bool, str]:
    if psx_shop.resolve_pet(result):
        return False, "PSX REBORN: OG pets cannot be crafted"
    if result in ings:
        return False, "The result cannot be one of the ingredients"
    if result in UNIQUE_IDS or any(c in UNIQUE_IDS for c in ings):
        return False, "Crown Tap cannot be crafted"
    floor = combined_floor(ings)
    if card_power(result) < floor:
        name = CARDS[result].name if result in CARDS else result
        return False, f"The result is weaker than the combined ingredients (minimum power {floor}; {name} has {card_power(result)})"
    return True, "ok"


def legal_results(ings: list[str], pool: list[str] | None = None) -> list[str]:
    pool = pool or CRAFT_POOL
    return [cid for cid in pool if recipe_legal(ings, cid)[0]]


def _pick_recipe(rng: random.Random, ings: list[str], coins: list[int], tag: str, rid: str) -> dict | None:
    opts = legal_results(ings)
    if not opts:
        return None
    return {
        "id": rid,
        "ings": ings,
        "coins": rng.choice(coins),
        "result": rng.choice(opts),
        "tag": tag,
        "kind": "rot",
    }


def rotating_recipes(now: float | None = None) -> list[dict]:
    rng = random.Random(window_id(now) * 9176 + 13)
    commons = list(COMMON_IDS)
    rares = list(RARE_IDS)
    out: list[dict] = []
    for _ in range(40):
        if len(out) >= 4:
            break
        n = len(out)
        if n == 0:
            rec = _pick_recipe(rng, rng.sample(commons, 3), [50, 70, 90], "Smelting", "r0")
        elif n == 1:
            rec = _pick_recipe(rng, rng.sample(commons, 3), [100, 130, 160], "Alloy", "r1")
        elif n == 2:
            rec = _pick_recipe(rng, rng.sample(commons, 2) + [rng.choice(rares)], [240, 300, 380], "Rare Earth", "r2")
        else:
            rec = _pick_recipe(rng, rng.sample(commons, 4), [420, 540, 680], "Legend", "r3")
        if rec:
            out.append(rec)
    return out


def recipes_for(user_id: int) -> list[dict]:
    recs = rotating_recipes()
    loader = getattr(db, "craft_custom_for", None)
    if not loader:
        return recs
    for row in loader(user_id):
        recs.append(
            {
                "id": f"c{row['id']}",
                "db_id": row["id"],
                "ings": list(row["ings"]),
                "coins": int(row["coins"]),
                "result": row["result"],
                "tag": row["tag"] or "custom",
                "kind": "personal" if row.get("target_user_id") else "global",
            }
        )
    return recs


def recipe_line(rec: dict) -> str:
    ings = " + ".join(f"{CARDS[i].emoji} {CARDS[i].name}" for i in rec["ings"] if i in CARDS)
    res = CARDS.get(rec["result"])
    res_s = f"{res.emoji} **{res.name}**" if res else rec["result"]
    mark = {"rot": "", "global": " · 🌍 everyone", "personal": " · 👤 you"}.get(rec.get("kind") or "rot", "")
    return f"**{rec['tag']}**{mark} · {ings} + **{rec['coins']}** 💰\n→ {res_s}"


def embed_for(user_id: int) -> discord.Embed:
    recs = recipes_for(user_id)
    user = db.get_user(user_id) or {}
    coins = user.get("coins") or 0
    inv = user.get("inventory") or {}
    e = discord.Embed(
        title="⚗️ Crafting Bench",
        description=(
            f"The same 4 recipes are shared by everyone. Changes <t:{window_end()}:R>\n"
            f"Balance **{coins}** 💰 · 3+ different cards + coins\n"
            f"The result must **not** be an ingredient and must be **at least as strong** as the combined ingredients."
        ),
        color=0x1ABC9C,
    )
    for i, rec in enumerate(recs[:20], 1):
        have = [f"{CARDS[cid].emoji}×{int(inv.get(cid) or 0)}" for cid in rec["ings"] if cid in CARDS]
        e.add_field(
            name=f"#{i} · {rec['tag']}",
            value=recipe_line(rec) + f"\nYou have: {' '.join(have)}",
            inline=False,
        )
    left = int(window_end() - time.time())
    e.set_footer(text=f"Rotation in {left // 60} minutes · ⚗️・crafts")
    return e


def try_craft(user_id: int, index: int) -> tuple[bool, str]:
    recs = recipes_for(user_id)
    if index < 0 or index >= len(recs):
        return False, "Recipe not found"
    rec = recs[index]
    ings = rec["ings"]
    if len(set(ings)) < 3:
        return False, "You need at least 3 different cards"
    custom = rec.get("kind") in ("global", "personal")
    if not custom:
        if any(c in EXCLUSIVE_IDS for c in ings) or rec["result"] in EXCLUSIVE_IDS:
            return False, "Exclusive cards cannot be crafted in the rotating recipes"
    ok, why = recipe_legal(ings, rec["result"])
    if not ok:
        return False, why
    user = db.get_user(user_id) or {}
    inv = user.get("inventory") or {}
    for cid in ings:
        if cid not in CARDS:
            return False, "Invalid recipe"
        if int(inv.get(cid) or 0) < 1:
            return False, f"You do not have {CARDS[cid].name}"
    if int(user.get("coins") or 0) < rec["coins"]:
        return False, f"You need {rec['coins']} 💰"
    if not db.take_coins(user_id, rec["coins"]):
        return False, "Coins could not be charged"
    for cid in ings:
        if not db.take_card(user_id, cid, 1):
            db.add_coins(user_id, rec["coins"])
            return False, "Cards could not be charged"
    qty = db.add_card(user_id, rec["result"], 1)
    res = CARDS[rec["result"]]
    return True, f"⚗️ Crafted {format_card(res)} · now ×{qty}"


def parse_ings(raw: str) -> list[str] | None:
    parts = [p.strip() for p in raw.replace(";", ",").replace("+", ",").replace("|", ",").split(",") if p.strip()]
    if len(parts) < 2:
        parts = [p for p in raw.replace("+", " ").split() if p.strip()]
    ids: list[str] = []
    for p in parts:
        card = resolve_card(p)
        if not card:
            return None
        ids.append(card.id)
    if len(set(ids)) < 3:
        return None
    return ids


class CraftView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        recs = recipes_for(owner_id)
        for i, rec in enumerate(recs[:20]):
            style = discord.ButtonStyle.success if rec.get("kind") == "rot" else discord.ButtonStyle.primary
            self.add_item(CraftButton(i, rec["tag"], style))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This is someone else's crafting bench. Use your own message or /crafts.", ephemeral=True)
            return False
        return True


class CraftButton(discord.ui.Button):
    def __init__(self, index: int, tag: str, style: discord.ButtonStyle):
        super().__init__(label=f"#{index + 1} {tag}"[:80], style=style)
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        ok, text = try_craft(interaction.user.id, self.index)
        view = CraftView(interaction.user.id)
        if not ok:
            await interaction.response.send_message(f"❌ {text}", ephemeral=True)
            try:
                await interaction.message.edit(embed=embed_for(interaction.user.id), view=view)
            except discord.HTTPException:
                pass
            return
        await interaction.response.edit_message(embed=embed_for(interaction.user.id), view=view)
        await interaction.followup.send(text, ephemeral=True)
