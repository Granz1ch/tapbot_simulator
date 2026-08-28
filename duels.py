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
            return None, f"Minimum stake: {MIN_COINS} coins"
        if n > MAX_COINS:
            return None, f"Maximum stake: {MAX_COINS} coins"
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
        return None, "Stake: `50`, `golden_tap`, or `wooden_tap 2`"
    if card.rarity == "exclusive":
        return None, "Exclusive cards (Titan / Crown) cannot be staked in a duel"
    if qty > 5:
        return None, "You can stake no more than 5 copies of the same card"
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

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        d = self.duel
        if interaction.user.id != d.b:
            await interaction.response.send_message("This challenge is not for you.", ephemeral=True)
            return
        if not d.live:
            await interaction.response.send_message("This challenge is no longer active.", ephemeral=True)
            return
        if not has_stake(d.b, d.stake):
            await interaction.response.send_message(f"You do not have the stake: {d.stake.label()}", ephemeral=True)
            return
        if not lock_stake(d.b, d.stake):
            await interaction.response.send_message("Could not lock the stake.", ephemeral=True)
            return
        d.locked_b = True
        for item in self.children:
            item.disabled = True  # type: ignore
        self.stop()
        await interaction.response.edit_message(
            content=f"⚔️ {self.names[d.a]} vs {self.names[d.b]} · pot {d.stake.label()} ×2",
            view=self,
        )
        await run_match(interaction.followup, interaction.message, d, self.names)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        d = self.duel
        if interaction.user.id not in (d.a, d.b):
            await interaction.response.send_message("This challenge is not for you.", ephemeral=True)
            return
        d.live = False
        if d.locked_a:
            give_stake(d.a, d.stake)
        _busy_drop(d.a, d.b)
        for item in self.children:
            item.disabled = True  # type: ignore
        self.stop()
        who = "declined" if interaction.user.id == d.b else "cancelled"
        await interaction.response.edit_message(content=f"🚫 {interaction.user.display_name} {who} the duel. The stake was returned.", view=self)


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
            await interaction.response.send_message("You are only a spectator.", ephemeral=True)
            return
        if time.monotonic() < self.armed_at:
            await interaction.response.send_message("Too early. Wait for the signal.", ephemeral=True)
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
                f"Round **{rnd}/3** · {names[d.a]} `{d.scores[d.a]}:{d.scores[d.b]}` {names[d.b]}\n"
                f"Wait… do not tap early."
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
                    content=f"⚡ {names[view.winner]} wins round {rnd}  ·  "
                    f"`{d.scores[d.a]}:{d.scores[d.b]}`"
                )
            else:
                await go.edit(content=f"💤 Round {rnd} is a draw — nobody tapped in time.")
            await asyncio.sleep(0.8)

        sa, sb = d.scores[d.a], d.scores[d.b]
        if sa == sb:
            give_stake(d.a, d.stake)
            give_stake(d.b, d.stake)
            text = f"🤝 Draw `{sa}:{sb}`. Stakes returned."
        else:
            winner = d.a if sa > sb else d.b
            give_stake(winner, d.stake, times=2)
            text = (
                f"🏆 **{names[winner]}** wins `{sa}:{sb}` and takes the {d.stake.label()} pot ×2"
            )
        await channel.send(text)
    except Exception:
        _refund_both(d)
        try:
            await channel.send("The duel failed — stakes were returned.")
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
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
    if rival.bot or rival.id == me.id:
        await interaction.response.send_message("You cannot duel a bot or yourself.", ephemeral=True)
        return
    if me.id in BUSY or rival.id in BUSY:
        await interaction.response.send_message("One of the players is already in a duel.", ephemeral=True)
        return
    stake, err = parse_stake(stake_raw)
    if not stake:
        await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        return
    if not has_stake(me.id, stake):
        await interaction.response.send_message(f"You do not have {stake.label()}", ephemeral=True)
        return
    if not lock_stake(me.id, stake):
        await interaction.response.send_message("Could not lock the stake.", ephemeral=True)
        return

    _busy_add(me.id, rival.id)
    duel = Duel(a=me.id, b=rival.id, stake=stake, locked_a=True)
    names = {me.id: me.display_name, rival.id: rival.display_name}
    view = ChallengeView(duel, names)
    extra = ""
    if stake.kind == "card":
        extra = f"\n{format_card(CARDS[stake.card_id])}"
    await interaction.response.send_message(
        f"⚔️ {me.mention} challenges {rival.mention}\n"
        f"Stake for each player: {stake.label()}{extra}\n"
        f"Best of 3 TAP rounds. The faster tap after the signal wins the round.\n"
        f"{rival.mention}, you have 45 seconds to respond.",
        view=view,
    )
