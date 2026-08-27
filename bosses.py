"""Server boss: tap the faucet. Exclusive 6th card only from Rare, one random hitter."""

from __future__ import annotations

import random
import time
from typing import Any

import discord

import database as db
from cards import BOSS_CARD_ID, CARDS, COMMON_IDS, format_card

TIERS = {
    "common": {
        "hp": 500,
        "title": "Common · Ржавый кран",
        "color": 0x95A5A6,
        "coin_pot": 120,
        "card_drops": 3,
        "exclusive": False,
        "weight": 50,
    },
    "uncommon": {
        "hp": 1500,
        "title": "Uncommon · Медный колосс",
        "color": 0x2ECC71,
        "coin_pot": 380,
        "card_drops": 6,
        "exclusive": False,
        "weight": 35,
    },
    "rare": {
        "hp": 2500,
        "title": "Rare · Титан",
        "color": 0x9B59B6,
        "coin_pot": 750,
        "card_drops": 4,
        "exclusive": True,
        "weight": 15,
    },
}

COOLDOWN = 35 * 60
_last_edit = 0.0


def _state() -> dict[str, Any]:
    return db.get_meta("boss") or {}


def _save(state: dict[str, Any]) -> None:
    db.set_meta("boss", state)


def hp_bar(hp: int, mx: int, width: int = 12) -> str:
    if mx <= 0:
        return "░" * width
    filled = max(0, min(width, round(width * hp / mx)))
    return "█" * filled + "░" * (width - filled)


def pick_tier() -> str:
    keys = list(TIERS)
    weights = [TIERS[k]["weight"] for k in keys]
    return random.choices(keys, weights=weights, k=1)[0]


def is_alive() -> bool:
    s = _state()
    return bool(s.get("alive") and int(s.get("hp") or 0) > 0)


def can_spawn(now: float | None = None) -> bool:
    s = _state()
    if s.get("alive"):
        return False
    now = now or time.time()
    return now >= float(s.get("next_at") or 0)


def start_boss(tier: str | None = None) -> dict[str, Any]:
    tier = tier if tier in TIERS else pick_tier()
    spec = TIERS[tier]
    state = {
        "alive": True,
        "tier": tier,
        "hp": spec["hp"],
        "max_hp": spec["hp"],
        "message_id": None,
        "started_at": time.time(),
        "next_at": 0,
    }
    db.boss_clear_hits()
    _save(state)
    return state


def hit(user_id: int, dmg: int) -> dict[str, Any]:
    s = _state()
    if not s.get("alive"):
        return {"ok": False, "reason": "none"}
    dmg = max(1, int(dmg))
    hp = max(0, int(s["hp"]) - dmg)
    s["hp"] = hp
    mine = db.boss_add_hit(user_id, dmg)
    killed = hp <= 0
    if killed:
        s["alive"] = False
        s["next_at"] = time.time() + COOLDOWN
    _save(s)
    return {"ok": True, "hp": hp, "max": s["max_hp"], "killed": killed, "mine": mine, "tier": s["tier"], "dmg": dmg}


def embed_for(state: dict[str, Any] | None = None) -> discord.Embed:
    s = state or _state()
    spec = TIERS.get(s.get("tier") or "common", TIERS["common"])
    hp, mx = int(s.get("hp") or 0), int(s.get("max_hp") or 1)
    e = discord.Embed(title=f"⚔️ Босс — {spec['title']}", color=spec["color"])
    e.description = f"`{hp_bar(hp, mx)}`  **{hp}/{mx}** HP"
    if spec["exclusive"]:
        e.add_field(
            name="Добыча Rare",
            value=f"монеты + обычные карты · **одному** случайному — {CARDS[BOSS_CARD_ID].emoji} Titan Tap",
            inline=False,
        )
    else:
        e.add_field(
            name="Добыча",
            value=f"банк **{spec['coin_pot']}** 💰 · **{spec['card_drops']}** common-карт(ы) участникам",
            inline=False,
        )
    top = db.boss_hits()[:5]
    if top:
        lines = [f"`#{i}` <@{r['user_id']}> — {r['damage']} dmg" for i, r in enumerate(top, 1)]
        e.add_field(name="Урон", value="\n".join(lines), inline=False)
    e.set_footer(text="Пиши в click — бьёшь босса. Следующий через ~35 мин после смерти.")
    return e


def payout() -> dict[str, Any]:
    s = _state()
    spec = TIERS[s.get("tier") or "common"]
    hits = db.boss_hits()
    total_dmg = sum(r["damage"] for r in hits) or 1
    coins_given: dict[int, int] = {}
    cards_given: dict[int, list[str]] = {}
    exclusive_to = None

    pot = int(spec["coin_pot"])
    leftover = pot
    for i, row in enumerate(hits):
        uid, dmg = row["user_id"], row["damage"]
        share = max(1, pot * dmg // total_dmg)
        if i == len(hits) - 1:
            share = max(1, leftover)
        leftover -= share
        db.add_coins(uid, share)
        coins_given[uid] = share

    n_cards = int(spec["card_drops"])
    if hits and n_cards:
        weights = [r["damage"] for r in hits]
        for _ in range(n_cards):
            pick = random.choices(hits, weights=weights, k=1)[0]
            cid = random.choice(COMMON_IDS)
            db.add_card(pick["user_id"], cid, 1)
            cards_given.setdefault(pick["user_id"], []).append(cid)

    if spec["exclusive"] and hits:
        weights = [r["damage"] for r in hits]
        exclusive_to = random.choices(hits, weights=weights, k=1)[0]["user_id"]
        db.add_card(exclusive_to, BOSS_CARD_ID, 1)

    return {
        "tier": s.get("tier"),
        "coins": coins_given,
        "cards": cards_given,
        "exclusive": exclusive_to,
        "spec": spec,
    }


def format_loot(result: dict[str, Any]) -> str:
    spec = result["spec"]
    lines = [f"💀 **{spec['title']}** пал."]
    if result["exclusive"]:
        card = CARDS[BOSS_CARD_ID]
        lines.append(
            f"🌑 {format_card(card)}\nдосталась <@{result['exclusive']}> (случайно среди бивших, вес = урон)"
        )
    top = sorted(result["coins"].items(), key=lambda x: -x[1])[:8]
    if top:
        bits = [f"<@{u}> +{c}💰" for u, c in top]
        lines.append("Монеты: " + " · ".join(bits))
    if result["cards"]:
        bits = []
        for u, ids in list(result["cards"].items())[:8]:
            names = ", ".join(CARDS[i].emoji for i in ids)
            bits.append(f"<@{u}> {names}")
        lines.append("Карты: " + " · ".join(bits))
    lines.append(f"Следующий босс примерно через {COOLDOWN // 60} мин.")
    return "\n".join(lines)


async def refresh_message(channel: discord.TextChannel, force: bool = False) -> None:
    global _last_edit
    now = time.monotonic()
    if not force and now - _last_edit < 4:
        return
    _last_edit = now
    s = _state()
    if not s:
        return
    embed = embed_for(s)
    mid = s.get("message_id")
    msg = None
    if mid:
        try:
            msg = await channel.fetch_message(int(mid))
        except discord.HTTPException:
            msg = None
    if msg:
        await msg.edit(embed=embed)
    else:
        msg = await channel.send(embed=embed)
        s["message_id"] = msg.id
        _save(s)


async def maybe_spawn(channel: discord.TextChannel, tier: str | None = None) -> bool:
    if not can_spawn() and tier is None:
        return False
    if _state().get("alive") and tier is None:
        return False
    start_boss(tier)
    await refresh_message(channel, force=True)
    return True
