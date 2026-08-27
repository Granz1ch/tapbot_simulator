"""UPD3: enchants, brawl, shards, referrals."""

from __future__ import annotations

import time

import discord

import database as db
from cards import CARDS, CROWN_CARD_ID, UNIQUE_IDS

SHARD_CAP = 10000
SHOP = {1.25: 400, 1.5: 900, 1.75: 1600, 2.0: 2800}
CHARM_WAIT = {1: 10 * 60, 2: 30 * 60, 3: 2 * 3600, 4: 8 * 3600}
CHARM_COINS_K = {1: 1.25, 2: 1.5, 3: 1.75, 4: 2.0}
CHARM_LUCK = {1: 1, 2: 2, 3: 3, 4: 4}
CHARM_PRICE = {
    "common": {1: 80, 2: 180, 3: 400, 4: 900},
    "rare": {1: 220, 2: 500, 3: 1100, 4: 2500},
}


def max_tier(rarity: str) -> int:
    if rarity == "common":
        return 2
    if rarity == "rare":
        return 4
    return 0


def charm_coins_k(user_id: int) -> float:
    t = db.enchant_best(user_id, "coins")
    return CHARM_COINS_K.get(t, 1.0)


def charm_luck_add(user_id: int) -> int:
    return CHARM_LUCK.get(db.enchant_best(user_id, "luck"), 0)


def shop_k(user_id: int, kind: str) -> float:
    return float(db.shard_slots(user_id).get(kind) or 1.0)


def brawl_until() -> float:
    return float(db.get_meta("brawl_until") or 0)


def brawl_active() -> bool:
    return time.time() < brawl_until()


def start_brawl(minutes: float) -> float:
    end = time.time() + max(0.25, minutes) * 60
    db.set_meta("brawl_until", end)
    return end


def start_enchant(user_id: int, card_id: str, kind: str, tier: int) -> tuple[bool, str]:
    card = CARDS.get(card_id)
    if not card:
        return False, "нет карты"
    if card_id in UNIQUE_IDS or card_id == CROWN_CARD_ID:
        return False, "Crown нельзя чарить"
    mx = max_tier(card.rarity)
    if tier < 1 or tier > mx:
        return False, f"для {card.rarity} максимум ступень {mx}"
    if kind not in ("coins", "luck"):
        return False, "coins или luck"
    if db.enchant_busy(user_id):
        return False, "уже идёт зачарование"
    price = CHARM_PRICE.get(card.rarity, {}).get(tier)
    if price is None:
        return False, "нет цены"
    if not db.take_card(user_id, card_id, 1):
        return False, "нет свободной карты (не зачарённой)"
    if not db.take_coins(user_id, price):
        db.add_card(user_id, card_id, 1)
        return False, f"нужно {price} 💰"
    ready = time.time() + CHARM_WAIT[tier]
    db.enchant_start(user_id, card_id, kind, tier, ready)
    return True, f"зачарование {card.emoji} **{card.name}** {kind} I{'I'* (tier-1)} · готово <t:{int(ready)}:R>"


def cancel_enchant(user_id: int) -> tuple[bool, str]:
    row = db.enchant_cancel(user_id)
    if not row:
        return False, "нечего отменять"
    db.add_card(user_id, row["card_id"], 1)
    return True, "карта возвращена, монеты не возвращаются"


def farm_shard(user_id: int, tap_gain: int) -> tuple[bool, str]:
    minted = db.shard_minted()
    if minted >= SHARD_CAP:
        return False, "пул осколков исчерпан (10 000)"
    cost = max(3, int(tap_gain))
    if not db.take_coins(user_id, cost):
        return False, f"нужно {cost} 💰"
    if not db.try_mint_shards(1):
        db.add_coins(user_id, cost)
        return False, "пул осколков исчерпан (10 000)"
    total = db.add_shards(user_id, 1)
    left = SHARD_CAP - db.shard_minted()
    return True, f"+1 💎 (у тебя {total}) · −{cost} 💰 · в пуле {left}"


def exchange_crystal(user_id: int) -> tuple[bool, str]:
    if db.shard_minted() + 2 > SHARD_CAP:
        return False, "в пуле нет 2 осколков"
    if not db.take_card(user_id, "crystal_tap", 1):
        return False, "нет Crystal Tap (обычного, не зачарованного)"
    if not db.try_mint_shards(2):
        db.add_card(user_id, "crystal_tap", 1)
        return False, "пул исчерпан"
    total = db.add_shards(user_id, 2)
    return True, f"−1 Crystal Tap → +2 💎 (у тебя {total})"


def buy_slot(user_id: int, kind: str, value: float) -> tuple[bool, str]:
    if value not in SHOP:
        return False, "ступень: 1.25 / 1.5 / 1.75 / 2"
    if kind not in ("coins", "luck"):
        return False, "coins или luck"
    cur = shop_k(user_id, kind)
    if value <= cur + 1e-9:
        return False, "уже есть такая или выше"
    price = SHOP[value] - SHOP.get(cur, 0)
    if price <= 0:
        price = SHOP[value]
    if not db.take_shards(user_id, price):
        return False, f"нужно {price} 💎"
    db.set_shard_slot(user_id, kind, value)
    return True, f"слот {kind} ×{value} · −{price} 💎"


def apply_ref(user_id: int, code: str) -> tuple[bool, str]:
    by_user = db.ref_by_code(code)
    if not by_user:
        return False, "нет такого кода"
    if not db.ref_bind(user_id, by_user):
        return False, "код уже привязан или это свой"
    return True, f"код принят. награда после первого тапа (PLAYER)."


def payout_ref(user_id: int) -> str | None:
    by_user = db.ref_pay_if_needed(user_id)
    if not by_user:
        return None
    db.add_coins(by_user, 50)
    db.add_coins(user_id, 25)
    extra = ""
    if db.try_mint_shards(2):
        db.add_shards(by_user, 2)
        extra = " · пригласившему +2 💎"
    return f"реферал: тебе +25 💰, <@{by_user}> +50 💰{extra}"


class EnchantView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.add_item(EnchantSelect(owner_id))
        self.add_item(CancelEnchant())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Не твоё меню.", ephemeral=True)
            return False
        return True


class EnchantSelect(discord.ui.Select):
    def __init__(self, owner_id: int):
        data = db.get_user(owner_id) or {}
        inv = data.get("inventory") or {}
        opts = []
        for cid, qty in list(inv.items())[:20]:
            card = CARDS.get(cid)
            if not card or card.rarity not in ("common", "rare") or qty < 1:
                continue
            opts.append(
                discord.SelectOption(
                    label=f"{card.name} ×{qty}"[:100],
                    value=cid,
                    emoji=card.emoji,
                    description=f"{card.rarity} · макс. {max_tier(card.rarity)} ст.",
                )
            )
        if not opts:
            opts = [discord.SelectOption(label="нет карт для чара", value="none")]
        super().__init__(placeholder="Карта для зачарования", options=opts)

    async def callback(self, interaction: discord.Interaction) -> None:
        cid = self.values[0]
        if cid == "none":
            await interaction.response.send_message("Нет подходящих карт.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Ступень и тип для {CARDS[cid].emoji} **{CARDS[cid].name}**",
            view=EnchantConfirm(interaction.user.id, cid),
            ephemeral=True,
        )


class EnchantConfirm(discord.ui.View):
    def __init__(self, owner_id: int, card_id: str):
        super().__init__(timeout=90)
        self.owner_id = owner_id
        self.card_id = card_id
        card = CARDS[card_id]
        for kind in ("coins", "luck"):
            for t in range(1, max_tier(card.rarity) + 1):
                label = f"{kind} {t}"
                self.add_item(GoEnchant(card_id, kind, t, label))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id


class GoEnchant(discord.ui.Button):
    def __init__(self, card_id: str, kind: str, tier: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.card_id = card_id
        self.kind = kind
        self.tier = tier

    async def callback(self, interaction: discord.Interaction) -> None:
        ok, text = start_enchant(interaction.user.id, self.card_id, self.kind, self.tier)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + text, ephemeral=True)


class CancelEnchant(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Отмена ожидания", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        ok, text = cancel_enchant(interaction.user.id)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + text, ephemeral=True)


def enchant_embed(user_id: int) -> discord.Embed:
    busy = db.enchant_busy(user_id)
    ready = db.enchant_list(user_id, ready_only=True)
    e = discord.Embed(title="✨ Чары", color=0xE8D44D)
    if busy:
        e.add_field(
            name="В процессе",
            value=f"{CARDS.get(busy['card_id']).emoji if busy['card_id'] in CARDS else busy['card_id']} "
            f"{busy['kind']} ст.{busy['tier']} · <t:{int(busy['ready_at'])}:R>",
            inline=False,
        )
    if ready:
        lines = [
            f"{CARDS[r['card_id']].emoji} {CARDS[r['card_id']].name} · {r['kind']} ст.{r['tier']}"
            for r in ready
            if r["card_id"] in CARDS
        ]
        e.add_field(name="Готовые", value="\n".join(lines)[:1000], inline=False)
    e.set_footer(text="Один процесс. Отмена — карта назад, монеты нет. Crown нельзя.")
    return e


class ShardShop(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        for kind in ("coins", "luck"):
            for val in (1.25, 1.5, 1.75, 2.0):
                self.add_item(BuyShard(kind, val))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Не твоё меню.", ephemeral=True)
            return False
        return True


class BuyShard(discord.ui.Button):
    def __init__(self, kind: str, val: float):
        super().__init__(label=f"{kind} ×{val}", style=discord.ButtonStyle.success)
        self.kind = kind
        self.val = val

    async def callback(self, interaction: discord.Interaction) -> None:
        ok, text = buy_slot(interaction.user.id, self.kind, self.val)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + text, ephemeral=True)


def shard_embed(user_id: int) -> discord.Embed:
    slots = db.shard_slots(user_id)
    e = discord.Embed(
        title="💎 Осколки",
        description=(
            f"У тебя **{db.shards_of(user_id)}** 💎 · пул {db.shard_minted()}/{SHARD_CAP}\n"
            f"слот монет ×{slots['coins']} · слот удачи ×{slots['luck']}"
        ),
        color=0x5DADE2,
    )
    return e
