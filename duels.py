"""Reaction duels: stake coins or a card, best of 3 TAP buttons, winner takes pot."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

import discord

import database as db
from cards import CARDS, format_card, resolve_card

BUSY: set[int] = set()
MIN_COINS = 5
MAX_COINS = 5000


@dataclass
class Stake:
    kind: str  # coins | card
    amount: int = 0
    card_id: str = ""

    def label(self) -> str:
        if self.kind == "coins":
            return f"**{self.amount}** 💰"
        card = CARDS[self.card_id]
        return f"{card.emoji} **{card.name}** ×{self.amount}"


def parse_stake(raw: str) -> tuple[Stake | None, str]:
    raw = raw.strip()
    if raw.isdigit():
        n = int(raw)
        if n < MIN_COINS:
            return None, f"минимум {MIN_COINS} монет"
        if n > MAX_COINS:
            return None, f"максимум {MAX_COINS} монет"
        return Stake("coins", amount=n), ""
    name, qty = raw, 1
    parts = raw.split()
    if len(parts) >= 2 and parts[-1].isdigit():
        qty = max(1, int(parts[-1]))
        name = " ".join(parts[:-1])
    elif len(parts) >= 2 and parts[-1].lower().startswith("x") and parts[-1][1:].isdigit():
        qty = max(1, int(parts[-1][1:]))
        name = " ".join(parts[:-1])
    card = resolve_card(name)
    if not card:
        return None, "ставка: `50` или `golden_tap` или `wooden_tap 2`"
    if card.rarity == "exclusive":
        return None, "эксклюзив (Titan / Crown) нельзя ставить на дуэль"
    if qty > 5:
        return None, "не больше 5 одинаковых карт в ставке"
    return Stake("card", amount=qty, card_id=card.id), ""


def has_stake(user_id: int, stake: Stake) -> bool:
    if stake.kind == "coins":
        u = db.get_user(user_id)
        return bool(u and u["coins"] >= stake.amount)
    u = db.get_user(user_id) or {}
    return int((u.get("inventory") or {}).get(stake.card_id) or 0) >= stake.amount


def lock_stake(user_id: int, stake: Stake) -> bool:
    if stake.kind == "coins":
        return db.take_coins(user_id, stake.amount)
    return db.take_card(user_id, stake.card_id, stake.amount)


def give_stake(user_id: int, stake: Stake, times: int = 1) -> None:
    if stake.kind == "coins":
        db.add_coins(user_id, stake.amount * times)
    else:
        db.add_card(user_id, stake.card_id, stake.amount * times)


@dataclass
class Duel:
    a: int
    b: int
    stake: Stake
    scores: dict[int, int] = field(default_factory=dict)
    locked_a: bool = False
    locked_b: bool = False
    live: bool = True
    opener: int | None = None

    def __post_init__(self) -> None:
        self.scores = {self.a: 0, self.b: 0}


def _busy_add(*ids: int) -> None:
    BUSY.update(ids)


def _busy_drop(*ids: int) -> None:
    for i in ids:
        BUSY.discard(i)


class ChallengeView(discord.ui.View):
    def __init__(self, duel: Duel, names: dict[int, str]):
        super().__init__(timeout=45)
        self.duel = duel
        self.names = names

    async def on_timeout(self) -> None:
        if not self.duel.live:
            return
        self.duel.live = False
        if self.duel.locked_a:
            give_stake(self.duel.a, self.duel.stake)
        _busy_drop(self.duel.a, self.duel.b)
        for item in self.children:
            item.disabled = True  # type: ignore

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        d = self.duel
        if interaction.user.id != d.b:
            await interaction.response.send_message("Это не твой вызов.", ephemeral=True)
            return
        if not d.live:
            await interaction.response.send_message("Вызов уже не действует.", ephemeral=True)
            return
        if not has_stake(d.b, d.stake):
            await interaction.response.send_message(f"Нет ставки: {d.stake.label()}", ephemeral=True)
            return
        if not lock_stake(d.b, d.stake):
            await interaction.response.send_message("Не смог списать ставку.", ephemeral=True)
            return
        d.locked_b = True
        for item in self.children:
            item.disabled = True  # type: ignore
        self.stop()
        await interaction.response.edit_message(
            content=f"⚔️ {self.names[d.a]} vs {self.names[d.b]} · банк {d.stake.label()} ×2",
            view=self,
        )
        await run_match(interaction.followup, interaction.message, d, self.names)

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        d = self.duel
        if interaction.user.id not in (d.a, d.b):
            await interaction.response.send_message("Не твой вызов.", ephemeral=True)
            return
        d.live = False
        if d.locked_a:
            give_stake(d.a, d.stake)
        _busy_drop(d.a, d.b)
        for item in self.children:
            item.disabled = True  # type: ignore
        self.stop()
        who = "отклонил" if interaction.user.id == d.b else "отменил"
        await interaction.response.edit_message(content=f"🚫 {interaction.user.display_name} {who} дуэль. Ставка возвращена.", view=self)


class TapRoundView(discord.ui.View):
    def __init__(self, duel: Duel, armed_at: float):
        super().__init__(timeout=8)
        self.duel = duel
        self.armed_at = armed_at
        self.winner: int | None = None
        self.lock = asyncio.Lock()

    @discord.ui.button(label="TAP", style=discord.ButtonStyle.primary, emoji="👆")
    async def tap(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        uid = interaction.user.id
        d = self.duel
        if uid not in (d.a, d.b):
            await interaction.response.send_message("Смотри со стороны.", ephemeral=True)
            return
        if time.monotonic() < self.armed_at:
            await interaction.response.send_message("Рано. Жди сигнал.", ephemeral=True)
            return
        async with self.lock:
            if self.winner is not None:
                await interaction.response.defer()
                return
            self.winner = uid
        for item in self.children:
            item.disabled = True  # type: ignore
        self.stop()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore


async def run_match(
    followup: discord.Webhook,
    seed: discord.Message | None,
    d: Duel,
    names: dict[int, str],
) -> None:
    channel = seed.channel if seed else None
    if channel is None:
        _refund_both(d)
        return
    try:
        for rnd in range(1, 4):
            if max(d.scores.values()) >= 2:
                break
            wait = random.uniform(1.4, 3.6)
            wait_msg = await channel.send(
                f"Раунд **{rnd}/3** · {names[d.a]} `{d.scores[d.a]}:{d.scores[d.b]}` {names[d.b]}\n"
                f"Ждите… не жми заранее."
            )
            await asyncio.sleep(wait)
            armed = time.monotonic()
            view = TapRoundView(d, armed)
            go = await channel.send("**TAP!**", view=view)
            try:
                await wait_msg.delete()
            except discord.HTTPException:
                pass
            await view.wait()
            if view.winner:
                d.scores[view.winner] += 1
                await go.edit(
                    content=f"⚡ {names[view.winner]} забирает раунд {rnd}  ·  "
                    f"`{d.scores[d.a]}:{d.scores[d.b]}`"
                )
            else:
                await go.edit(content=f"💤 Раунд {rnd} в ничью — никто не успел.")
            await asyncio.sleep(0.8)

        sa, sb = d.scores[d.a], d.scores[d.b]
        if sa == sb:
            give_stake(d.a, d.stake)
            give_stake(d.b, d.stake)
            text = f"🤝 Ничья `{sa}:{sb}`. Ставки возвращены."
        else:
            winner = d.a if sa > sb else d.b
            give_stake(winner, d.stake, times=2)
            text = (
                f"🏆 **{names[winner]}** побеждает `{sa}:{sb}` и забирает банк {d.stake.label()} ×2"
            )
        await channel.send(text)
    except Exception:
        _refund_both(d)
        try:
            await channel.send("Дуэль сломалась — ставки возвращены.")
        except discord.HTTPException:
            pass
    finally:
        d.live = False
        _busy_drop(d.a, d.b)


def _refund_both(d: Duel) -> None:
    if d.locked_a:
        give_stake(d.a, d.stake)
        d.locked_a = False
    if d.locked_b:
        give_stake(d.b, d.stake)
        d.locked_b = False


async def start_challenge(interaction: discord.Interaction, rival: discord.Member, stake_raw: str) -> None:
    me = interaction.user
    if not isinstance(me, discord.Member):
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return
    if rival.bot or rival.id == me.id:
        await interaction.response.send_message("Нельзя с ботом или собой.", ephemeral=True)
        return
    if me.id in BUSY or rival.id in BUSY:
        await interaction.response.send_message("Кто-то уже в дуэли.", ephemeral=True)
        return
    stake, err = parse_stake(stake_raw)
    if not stake:
        await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        return
    if not has_stake(me.id, stake):
        await interaction.response.send_message(f"У тебя нет {stake.label()}", ephemeral=True)
        return
    if not lock_stake(me.id, stake):
        await interaction.response.send_message("Не списал ставку.", ephemeral=True)
        return

    _busy_add(me.id, rival.id)
    duel = Duel(a=me.id, b=rival.id, stake=stake, locked_a=True)
    names = {me.id: me.display_name, rival.id: rival.display_name}
    view = ChallengeView(duel, names)
    extra = ""
    if stake.kind == "card":
        extra = f"\n{format_card(CARDS[stake.card_id])}"
    await interaction.response.send_message(
        f"⚔️ {me.mention} вызывает {rival.mention}\n"
        f"Ставка с каждой стороны: {stake.label()}{extra}\n"
        f"3 раунда TAP. Кто быстрее жмёт после сигнала — тот раунд.\n"
        f"{rival.mention}, 45 сек на ответ.",
        view=view,
    )
