"""Coin shop: persistent tap/luck/boss/haste upgrades."""

from __future__ import annotations

import discord

import database as db

CATALOG = {
    "power": {
        "title": "Tap Power",
        "emoji": "💪",
        "max": 8,
        "desc": "+1 coin per tap for each level",
        "costs": [80, 160, 280, 450, 700, 1100, 1700, 2600],
    },
    "luck": {
        "title": "Luck Shard",
        "emoji": "🍀",
        "max": 4,
        "desc": "+1 luck (unlocks rare cards in rolls)",
        "costs": [200, 500, 1100, 2200],
    },
    "boss": {
        "title": "Boss Damage",
        "emoji": "⚔️",
        "max": 6,
        "desc": "+1 boss damage per level",
        "costs": [100, 180, 300, 480, 750, 1200],
    },
    "haste": {
        "title": "Haste",
        "emoji": "⚡",
        "max": 4,
        "desc": "−0.3 seconds from tap cooldown (minimum 0.8s)",
        "costs": [150, 320, 640, 1200],
    },
}


def cost_for(key: str, level: int) -> int | None:
    spec = CATALOG[key]
    if level >= spec["max"]:
        return None
    return spec["costs"][level]


def embed_for(user_id: int) -> discord.Embed:
    coins = (db.get_user(user_id) or {}).get("coins") or 0
    e = discord.Embed(
        title="🔧 Upgrades",
        description=f"Balance **{coins}** 💰 · click a button to buy a level",
        color=0xE67E22,
    )
    for key, spec in CATALOG.items():
        lv = db.upgrade_level(user_id, key)
        nxt = cost_for(key, lv)
        bar = "●" * lv + "○" * (spec["max"] - lv)
        tail = "MAX" if nxt is None else f"next **{nxt}** 💰"
        e.add_field(
            name=f"{spec['emoji']} {spec['title']}  `{bar}`",
            value=f"{spec['desc']}\nlevel **{lv}/{spec['max']}** · {tail}",
            inline=False,
        )
    e.set_footer(text="Only available in 🔧・upgrades")
    return e


class UpgradeView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        for key, spec in CATALOG.items():
            self.add_item(BuyButton(key, spec["emoji"], spec["title"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This is someone else's shop. Use your own message or /upgrade.", ephemeral=True)
            return False
        return True


class BuyButton(discord.ui.Button):
    def __init__(self, key: str, emoji: str, title: str):
        super().__init__(label=title, emoji=emoji, style=discord.ButtonStyle.primary, custom_id=f"up_{key}")
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        lv = db.upgrade_level(uid, self.key)
        price = cost_for(self.key, lv)
        if price is None:
            await interaction.response.send_message("Already at maximum.", ephemeral=True)
            return
        if not db.take_coins(uid, price):
            await interaction.response.send_message(f"You need **{price}** 💰", ephemeral=True)
            return
        db.set_upgrade_level(uid, self.key, lv + 1)
        view = UpgradeView(uid)
        await interaction.response.edit_message(embed=embed_for(uid), view=view)


async def send_panel(destination, user_id: int, *, ephemeral: bool = False) -> None:
    view = UpgradeView(user_id)
    kwargs = {"embed": embed_for(user_id), "view": view}
    if ephemeral:
        kwargs["ephemeral"] = True
    if hasattr(destination, "response") and not destination.response.is_done():
        await destination.response.send_message(**kwargs)
    else:
        await destination.send(embed=embed_for(user_id), view=view, delete_after=90)
