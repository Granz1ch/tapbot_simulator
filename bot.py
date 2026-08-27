#!/usr/bin/env python3
"""Tap Simulator — Discord placement for the tap/card economy."""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

import ai_admin
import database as db
import bosses
import crafts
import duels
import upgrades
import upd3
from cards import BOSS_CARD_ID, CARDS, COMMON_IDS, CROWN_CARD_ID, EXCLUSIVE_IDS, RARE_IDS, SHIELD_CARD_ID, UNIQUE_IDS, card_image, format_card, resolve_card, sell_price
from datetime import date

def remember(member: discord.Member) -> None:
    try:
        url = member.display_avatar.replace(size=64).url
    except Exception:
        url = None
    db.cache_identity(member.id, member.display_name, url)


def collection_bonus(inventory: dict) -> int:
    owned = {cid for cid, q in (inventory or {}).items() if q > 0}
    extra = 0
    if set(COMMON_IDS) <= owned:
        extra += 1
    extra += sum(1 for cid in RARE_IDS if cid in owned)
    extra += 2 * sum(1 for cid in EXCLUSIVE_IDS if cid in owned)
    return extra


def card_file(card_id: str) -> discord.File | None:
    path = card_image(card_id)
    if not path:
        return None
    return discord.File(path, filename=f"{card_id}.jpg")

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

GIVE_RE = re.compile(
    r"^/give\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
SELL_RE = re.compile(r"^/sell\s+(.+)$", re.IGNORECASE | re.DOTALL)
COINS_RE = re.compile(r"^(\d+)\s*(coins?|монет[аыу]?|coin)?$", re.IGNORECASE)
MENTION_RE = re.compile(r"<@!?(\d+)>")
ROLE_ALIASES = {
    "x5_coins": "x5_coins",
    "x5coins": "x5_coins",
    "x5 coin": "x5_coins",
    "x5 coins": "x5_coins",
    "x2_coins": "x2_coins",
    "x2coins": "x2_coins",
    "x2 coin": "x2_coins",
    "x2 coins": "x2_coins",
    "x5_luck": "x5_luck",
    "x5luck": "x5_luck",
    "x5 luck": "x5_luck",
    "x2_luck": "x2_luck",
    "x2luck": "x2_luck",
    "x2 luck": "x2_luck",
    "player": "player",
    "top1": "top1",
    "top-1": "top1",
}
DURATION_RE = re.compile(r"(\d+)\s*(s|sec|secs|секунд|сек|m|min|mins|минут|ч|h|hour|hours|час|часа|часов|d|day|days|дн|дня|дней)?", re.I)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"Missing {CONFIG_PATH}. Copy config.example.json to config.json and fill IDs."
        )
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()


def ch(name: str) -> int:
    raw = CFG.get("channels", {}).get(name)
    if raw in (None, "", 0, "0", "-", "—", "–"):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def channel_style(name: str) -> str:
    return str((CFG.get("channel_names") or {}).get(name) or name)


def role_id(key: str) -> int:
    return int(CFG["roles"][key])


def has_role(member: discord.Member, key: str) -> bool:
    rid = role_id(key)
    if not rid:
        return False
    return any(r.id == rid for r in member.roles)


def is_admin(member: discord.Member | None) -> bool:
    if member is None:
        return False
    return has_role(member, "admin") or member.guild_permissions.administrator


def coin_mult(member: discord.Member) -> float:
    if has_role(member, "x5_coins"):
        m = 5.0
    elif has_role(member, "x2_coins"):
        m = 2.0
    else:
        m = 1.0
    if db.owns_unique(member.id, CROWN_CARD_ID):
        m = max(m, 2.0)
    m = max(m, upd3.shop_k(member.id, "coins"))
    return m * upd3.charm_coins_k(member.id)


def luck_mult(member: discord.Member) -> int:
    if has_role(member, "x5_luck"):
        m = 5
    elif has_role(member, "x2_luck"):
        m = 2
    else:
        m = 1
    if db.owns_unique(member.id, CROWN_CARD_ID):
        m = max(m, 2)
    shop = upd3.shop_k(member.id, "luck")
    extra = {1.25: 1, 1.5: 2, 1.75: 3, 2.0: 4}.get(shop, 0)
    m = max(m, extra) if extra else m
    m += db.upgrade_level(member.id, "luck")
    m += upd3.charm_luck_add(member.id)
    return m


def tap_gain_for(member: discord.Member) -> int:
    base = int(CFG["game"]["tap_coins_base"]) + db.upgrade_level(member.id, "power")
    user = db.get_user(member.id) or {}
    g = int(base * coin_mult(member)) + collection_bonus(user.get("inventory"))
    return max(1, g)


def has_shield(user_id: int) -> bool:
    inv = (db.get_user(user_id) or {}).get("inventory") or {}
    return int(inv.get(SHIELD_CARD_ID) or 0) > 0


def require_channel(interaction: discord.Interaction, key: str) -> str | None:
    cid = ch(key)
    pretty = channel_style(key)
    if not cid:
        return f"Канал **{pretty}** ещё не привязан: в config.json у `channels.{key}` стоит прочерк. Создай канал и вставь ID."
    if interaction.channel_id != cid:
        return f"Только в <#{cid}> (`{pretty}`)."
    return None


def pick_card(luck: int) -> str:
    if luck <= 1:
        return random.choice(COMMON_IDS)
    pool = list(COMMON_IDS)
    for _ in range(luck):
        pool.extend(RARE_IDS)
    return random.choice(pool)


class TapBot(commands.Bot):
    def __init__(self) -> None:
        want_members = bool(CFG.get("intents_members", True))
        want_content = bool(CFG.get("intents_message_content", True))
        intents = discord.Intents(
            guilds=True,
            guild_messages=True,
            guild_reactions=True,
            message_content=want_content,
            members=want_members,
            presences=False,
        )
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.cooldowns: dict[tuple[int, str], float] = {}
        self.combo: dict[int, tuple[float, int]] = {}
        self.guild_obj = discord.Object(id=int(CFG["guild_id"])) if CFG.get("guild_id") else None
        interval = max(60, int(CFG["game"]["leaderboard_interval_seconds"]))
        self.leaderboard_loop.change_interval(seconds=interval)

    async def setup_hook(self) -> None:
        db.init_db()
        self.leaderboard_loop.start()
        self.expire_roles_loop.start()
        self.boss_loop.start()
        self.enchant_loop.start()

    @tasks.loop(seconds=30)
    async def expire_roles_loop(self) -> None:
        due = db.due_timed_roles(time.time())
        if not due:
            return
        guild = self.get_guild(int(CFG["guild_id"]))
        if not guild:
            return
        for item in due:
            member = guild.get_member(item["user_id"])
            rid = role_id(item["role_key"]) if item["role_key"] in CFG["roles"] else 0
            role = guild.get_role(rid) if rid else None
            if member and role:
                try:
                    await member.remove_roles(role, reason="Timed event role expired")
                except discord.HTTPException:
                    pass
            db.delete_timed_role(item["user_id"], item["role_key"])

    @expire_roles_loop.before_loop
    async def before_expire(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=3600)
    async def leaderboard_loop(self) -> None:
        await refresh_leaderboard()

    @leaderboard_loop.before_loop
    async def before_lb(self) -> None:
        await self.wait_until_ready()
        await asyncio.sleep(5)
        await refresh_leaderboard()

    @tasks.loop(seconds=20)
    async def boss_loop(self) -> None:
        channel = self.get_channel(ch("tap"))
        if isinstance(channel, discord.TextChannel):
            await bosses.maybe_spawn(channel)

    @boss_loop.before_loop
    async def before_boss(self) -> None:
        await self.wait_until_ready()
        await asyncio.sleep(8)

    @tasks.loop(seconds=15)
    async def enchant_loop(self) -> None:
        db.enchant_finish_due(time.time())

    @enchant_loop.before_loop
    async def before_enchant(self) -> None:
        await self.wait_until_ready()

    async def on_ready(self) -> None:
        print(f"Tap Simulator online as {self.user} ({self.user.id})", flush=True)
        await self.change_presence(activity=discord.Game(name="Tap Simulator"))
        guild = None
        gid = int(CFG.get("guild_id") or 0)
        if gid:
            guild = self.get_guild(gid)
        if guild is None and self.guilds:
            guild = self.guilds[0]
            CFG["guild_id"] = guild.id
            print(f"Using guild {guild.name} ({guild.id})")
        if guild:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Slash commands synced to {guild.id}")

    def on_cd(self, user_id: int, kind: str, seconds: float) -> float:
        now = time.monotonic()
        key = (user_id, kind)
        until = self.cooldowns.get(key, 0)
        if now < until:
            return until - now
        self.cooldowns[key] = now + seconds
        return 0.0


bot = TapBot()


def remember(member: discord.Member) -> None:
    try:
        url = member.display_avatar.replace(size=64).url
    except Exception:
        url = None
    db.cache_identity(member.id, member.display_name, url)


def collection_bonus(inventory: dict | None) -> int:
    owned = {cid for cid, q in (inventory or {}).items() if q > 0}
    extra = 0
    if set(COMMON_IDS) <= owned:
        extra += 1
    extra += sum(1 for cid in RARE_IDS if cid in owned)
    extra += 2 * sum(1 for cid in EXCLUSIVE_IDS if cid in owned)
    return extra


def card_file(card_id: str) -> discord.File | None:
    path = card_image(card_id)
    if not path:
        return None
    return discord.File(path, filename=f"{card_id}.jpg")


def profile_embed(member: discord.abc.User, data: dict | None) -> discord.Embed:
    data = data or {"coins": 0, "taps": 0, "spins": 0, "inventory": {}}
    e = discord.Embed(title=f"Tap Simulator — {member.display_name}", color=0xF5C542)
    e.add_field(name="💰 Coins", value=str(data["coins"]))
    e.add_field(name="👆 Taps", value=str(data["taps"]))
    e.add_field(name="🎰 Spins", value=str(data["spins"]))
    lines = []
    for cid, qty in (data.get("inventory") or {}).items():
        card = CARDS.get(cid)
        if card:
            lines.append(f"{card.emoji} {card.name} ×{qty}")
    for u in data.get("unique_cards") or []:
        lines.append(f"⚜️ Crown Tap `#{u['serial']}` · {u['inscribed_name']}")
    e.add_field(name="🃏 Cards", value="\n".join(lines) if lines else "— empty —", inline=False)
    bonus = collection_bonus(data.get("inventory"))
    if bonus:
        e.add_field(name="📚 Коллекция", value=f"+{bonus} 💰 к каждому тапу", inline=False)
    if data.get("daily_streak"):
        e.add_field(name="🔥 Daily streak", value=str(data["daily_streak"]))
    if getattr(member, "display_avatar", None):
        e.set_thumbnail(url=member.display_avatar.url)
    featured = None
    inv = data.get("inventory") or {}
    for cid in list(EXCLUSIVE_IDS) + list(RARE_IDS) + list(COMMON_IDS):
        if inv.get(cid):
            featured = cid
            break
    img = card_image(featured) if featured else None
    if img:
        e.set_image(url=f"attachment://{featured}.jpg")
    return e


async def ensure_player_role(member: discord.Member) -> None:
    rid = role_id("player")
    if rid and not has_role(member, "player"):
        role = member.guild.get_role(rid)
        if role:
            try:
                await member.add_roles(role, reason="Joined Tap Simulator")
            except discord.HTTPException:
                pass


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return

    cid = message.channel.id
    member = message.author
    if not isinstance(member, discord.Member):
        return

    # --- Admin AI channel ---
    if cid == ch("admin_ai"):
        if not is_admin(member):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return
        await handle_admin_ai(message)
        return

    # --- Tap channel ---
    if cid == ch("tap"):
        await handle_tap(message, member)
        return

    # --- Spin channel ---
    if cid == ch("spin"):
        await handle_spin(message, member)
        return

    # --- Trade channel: /give as reply ---
    if cid == ch("trade"):
        await handle_trade_message(message, member)
        return

    # --- Promo codes: one word = code ---
    if cid and cid == ch("promocodes"):
        await handle_promo_message(message, member)
        return

    if cid and cid == ch("upgrade"):
        await ensure_player_role(member)
        if bot.on_cd(member.id, "upgrade_ui", 10) > 0:
            return
        await message.channel.send(
            embed=upgrades.embed_for(member.id),
            view=upgrades.UpgradeView(member.id),
            delete_after=90,
        )
        return

    if cid and cid == ch("craft"):
        await ensure_player_role(member)
        if bot.on_cd(member.id, "craft_ui", 10) > 0:
            return
        await message.channel.send(
            embed=crafts.embed_for(member.id),
            view=crafts.CraftView(member.id),
            delete_after=90,
        )
        return

    if cid and cid == ch("enchant"):
        await ensure_player_role(member)
        if bot.on_cd(member.id, "enchant_ui", 10) > 0:
            return
        await message.channel.send(
            embed=upd3.enchant_embed(member.id),
            view=upd3.EnchantView(member.id),
            delete_after=90,
        )
        return

    if cid and cid == ch("shards"):
        await handle_shards(message, member)
        return

    if cid and cid == ch("crystal_ex"):
        await handle_crystal_ex(message, member)
        return

    if cid and cid == ch("ref"):
        await handle_ref_message(message, member)
        return

    await bot.process_commands(message)


async def handle_tap(message: discord.Message, member: discord.Member) -> None:
    haste = db.upgrade_level(member.id, "haste")
    cd_s = max(0.8, float(CFG["game"]["tap_cooldown_seconds"]) - 0.3 * haste)
    cd = bot.on_cd(member.id, "tap", cd_s)
    if cd > 0:
        return
    await ensure_player_role(member)
    remember(member)
    note = upd3.payout_ref(member.id)
    if note:
        try:
            await message.channel.send(note, delete_after=30)
        except discord.HTTPException:
            pass
    gained = tap_gain_for(member)
    # Драка: reply на игрока вместо обычного тапа
    if upd3.brawl_active() and message.reference and message.reference.message_id:
        target = message.reference.resolved
        if target is None:
            try:
                target = await message.channel.fetch_message(message.reference.message_id)
            except discord.HTTPException:
                target = None
        victim = getattr(target, "author", None) if target else None
        if victim and not victim.bot and victim.id != member.id:
            if has_shield(member.id) or has_shield(victim.id):
                try:
                    await message.add_reaction("🛡️")
                except discord.HTTPException:
                    pass
            else:
                took = db.steal_coins(victim.id, member.id, gained)
                db.bump_taps(member.id)
                try:
                    await message.add_reaction("⚔️" if took else "💨")
                except discord.HTTPException:
                    pass
                if took:
                    await message.channel.send(
                        f"⚔️ {member.mention} забирает **{took}** 💰 у {victim.mention}",
                        delete_after=8,
                    )
                return
    now = time.monotonic()
    last, streak = bot.combo.get(member.id, (0.0, 0))
    streak = streak + 1 if now - last < 10 else 1
    bot.combo[member.id] = (now, streak)
    if streak and streak % 10 == 0:
        gained += 5
    total = db.add_coins(member.id, gained)
    db.bump_taps(member.id)
    if bosses.is_alive():
        loot = bosses.hit(member.id, gained)
        try:
            if loot:
                await message.channel.send(bosses.format_loot(loot))
            else:
                await bosses.refresh_message(message.channel)
        except discord.HTTPException:
            pass
    try:
        await message.add_reaction("💰")
    except discord.HTTPException:
        pass
    # quiet economy: occasional confirmation
    if streak >= 5 or random.random() < 0.08:
        extra = f" · combo x{streak}" if streak >= 3 else ""
        await message.channel.send(
            f"{member.mention} +{gained} 💰 (всего {total}){extra}",
            delete_after=8,
        )


async def handle_spin(message: discord.Message, member: discord.Member) -> None:
    cd = bot.on_cd(member.id, "spin", float(CFG["game"]["spin_cooldown_seconds"]))
    if cd > 0:
        try:
            await message.add_reaction("⏳")
        except discord.HTTPException:
            pass
        return
    await ensure_player_role(member)
    luck = luck_mult(member)
    card_id = pick_card(luck)
    card = CARDS[card_id]
    qty = db.add_card(member.id, card_id, 1)
    db.bump_spins(member.id)
    color = 0x9B59B6 if card.rarity == "rare" else 0x3498DB
    e = discord.Embed(
        title="Прокрутка карточки",
        description=format_card(card) + f"\nТеперь у тебя: **×{qty}**",
        color=color,
    )
    footer = {5: "x5 LUCK", 2: "x2 LUCK"}.get(luck, "default luck — только 5 common")
    e.set_footer(text=footer)
    remember(member)
    f = card_file(card_id)
    if f:
        e.set_image(url=f"attachment://{card_id}.jpg")
        await message.reply(embed=e, file=f, mention_author=False)
    else:
        await message.reply(embed=e, mention_author=False)


async def handle_trade_message(message: discord.Message, member: discord.Member) -> None:
    raw = message.content.strip()
    sm = SELL_RE.match(raw)
    if sm:
        ok, text = do_sell(member.id, sm.group(1).strip())
        await message.reply(text, mention_author=False)
        return
    m = GIVE_RE.match(raw)
    if not m:
        return
    if not message.reference or not message.reference.message_id:
        await message.reply("Ответь (`Reply`) на сообщение игрока, которому отдаёшь.", mention_author=False)
        return
    try:
        target_msg = message.reference.resolved or await message.channel.fetch_message(
            message.reference.message_id
        )
    except discord.HTTPException:
        await message.reply("Не нашёл исходное сообщение.", mention_author=False)
        return
    target = target_msg.author
    if target.bot or target.id == member.id:
        await message.reply("Нельзя отдать боту или себе.", mention_author=False)
        return
    payload = m.group(1).strip()
    ok, text = do_give(member.id, target.id, payload)
    await message.reply(text, mention_author=False)


def do_give(from_id: int, to_id: int, payload: str) -> tuple[bool, str]:
    cm = COINS_RE.match(payload.replace("_", " ").strip())
    if cm:
        amount = int(cm.group(1))
        ok, err = db.transfer_coins(from_id, to_id, amount)
        if not ok:
            return False, f"❌ {err}"
        return True, f"✅ Передано **{amount}** 💰 → <@{to_id}>"

    # card [xN]
    parts = payload.split()
    qty = 1
    name = payload
    if len(parts) >= 2 and parts[-1].lower().startswith("x") and parts[-1][1:].isdigit():
        qty = int(parts[-1][1:])
        name = " ".join(parts[:-1])
    elif len(parts) >= 2 and parts[-1].isdigit():
        qty = int(parts[-1])
        name = " ".join(parts[:-1])
    card = resolve_card(name)
    if not card:
        return False, "❌ Не понял предмет. Пример: `/give 50` или `/give golden_tap` или `/give neon_tap x2`"
    extra = ""
    if card.id in UNIQUE_IDS:
        owned = db.unique_of(from_id, card.id)
        if not owned:
            return False, "❌ нет этой уникальной карты"
        u = owned[0]
        extra = f" · `#{u['serial']}` {u['inscribed_name']} (надпись навсегда)"
        qty = 1
    ok, err = db.transfer_card(from_id, to_id, card.id, qty)
    if not ok:
        moved = db.transfer_enchanted(from_id, to_id, card.id, qty)
        if moved:
            extra = extra or " · с чарами"
            return True, f"✅ Передано {card.emoji} **{card.name}** ×{moved}{extra} → <@{to_id}>"
        return False, f"❌ {err}"
    return True, f"✅ Передано {card.emoji} **{card.name}** ×{qty}{extra} → <@{to_id}>"


def parse_card_qty(payload: str) -> tuple[str, int]:
    parts = payload.split()
    qty = 1
    name = payload
    if len(parts) >= 2 and parts[-1].lower().startswith("x") and parts[-1][1:].isdigit():
        qty = int(parts[-1][1:])
        name = " ".join(parts[:-1])
    elif len(parts) >= 2 and parts[-1].isdigit():
        qty = int(parts[-1])
        name = " ".join(parts[:-1])
    return name, max(1, qty)


def do_sell(user_id: int, payload: str) -> tuple[bool, str]:
    name, qty = parse_card_qty(payload)
    card = resolve_card(name)
    if not card:
        return False, "❌ карта? пример: `/sell wooden_tap` или `/sell crystal_tap 3`"
    price = sell_price(card.id)
    if price is None:
        return False, "❌ rare нельзя продать. только первые 5 common, до 50 💰 за штуку"
    if not db.take_card(user_id, card.id, qty):
        return False, "❌ нет столько карт"
    total = price * qty
    coins = db.add_coins(user_id, total)
    return True, f"✅ Продано {card.emoji} **{card.name}** ×{qty} → **+{total}** 💰 (баланс {coins})"


def role_key_from_mention(text: str) -> str | None:
    for m in re.finditer(r"<@&(\d+)>", text):
        rid = int(m.group(1))
        for key, val in CFG["roles"].items():
            if int(val) == rid:
                return key
    return None


def parse_duration(text: str | None) -> int | None:
    if not text:
        return None
    m = DURATION_RE.search(str(text).strip())
    if not m:
        return None
    n = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit in ("m", "min", "mins", "минут"):
        return n * 60
    if unit in ("h", "hour", "hours", "ч", "час", "часа", "часов"):
        return n * 3600
    if unit in ("d", "day", "days", "дн", "дня", "дней"):
        return n * 86400
    return n


def resolve_role_key(text: str) -> str | None:
    mentioned = role_key_from_mention(text)
    if mentioned:
        return mentioned
    q = text.strip().lower().replace("-", "_")
    return ROLE_ALIASES.get(q) or ROLE_ALIASES.get(q.replace("_", " "))


def hierarchy_error(guild: discord.Guild, key: str) -> str | None:
    me = guild.me
    if me is None:
        return "бот не в кэше гильдии"
    if not me.guild_permissions.manage_roles:
        return "у бота нет права Manage Roles — включи в роли бота"
    rid = role_id(key) if key in CFG["roles"] else 0
    role = guild.get_role(rid) if rid else None
    if not role:
        return f"роль `{key}` не найдена"
    if role >= me.top_role:
        return (
            f"роль **{role.name}** выше или равна роли бота **{me.top_role.name}**. "
            f"В настройках сервера перетащи роль бота ВЫШЕ {role.name}"
        )
    if role.is_default():
        return "нельзя выдать @everyone"
    return None


async def iter_targets(guild: discord.Guild):
    seen: set[int] = set()
    bot_id = bot.user.id if bot.user else 0
    for member in list(guild.members):
        if member.bot or member.id == bot_id:
            continue
        seen.add(member.id)
        yield member
    if getattr(bot.intents, "members", False):
        try:
            async for member in guild.fetch_members(limit=None):
                if member.bot or member.id in seen:
                    continue
                seen.add(member.id)
                yield member
        except (discord.HTTPException, discord.ClientException):
            pass
    for name in ("tap", "spin", "trade", "admin_ai", "leaderboard"):
        channel = guild.get_channel(ch(name))
        if not isinstance(channel, discord.TextChannel):
            continue
        try:
            async for msg in channel.history(limit=100):
                author = msg.author
                if author.bot or author.id in seen:
                    continue
                member = author if isinstance(author, discord.Member) else guild.get_member(author.id)
                if member is None:
                    continue
                seen.add(member.id)
                yield member
        except discord.HTTPException:
            pass
    for uid in db.all_user_ids():
        if uid in seen or uid == bot_id:
            continue
        try:
            member = await guild.fetch_member(uid)
        except discord.HTTPException:
            continue
        if member.bot:
            continue
        seen.add(member.id)
        yield member


def parse_promo_admin(low: str, raw: str) -> dict | None:
    if re.search(r"\b(промокоды|промокод|список промо|list.?promo)\b", low) and not re.search(
        r"\b(созда|create|сделай|новый)\b", low
    ):
        if re.search(r"\b(выкл|отключ|disable|удал|delete)\b", low):
            m = re.search(r"(?:промокод(?:у|а)?|promo)\s+([A-Za-z0-9_-]{1,32})", raw, re.I)
            if m:
                return {"action": "disable_promo", "code": m.group(1)}
        return {"action": "list_promos"}

    m = re.search(
        r"(?:создай|создать|сделай|create|новый)\s+(?:промокод|промо|promo(?:code)?)\s+([A-Za-z0-9_-]{1,32})",
        raw,
        re.I,
    )
    if not m:
        m = re.search(r"(?:промокод|promo)\s+([A-Za-z0-9_-]{1,32})", raw, re.I)
        if m and not re.search(r"\b(созда|create|сделай|настрой|измени|update)\b", low):
            m = None
    if not m:
        return None

    code = m.group(1)
    action = "update_promo" if re.search(r"\b(измени|настрой|update|поменя)\b", low) else "create_promo"
    uses = None
    mu = re.search(r"(\d+)\s*(активац|uses?|раз)", low)
    if mu:
        uses = int(mu.group(1))
    seconds = None
    mf = re.search(r"(на|for)\s+(\d+\s*\w*)", low)
    if mf:
        seconds = parse_duration(mf.group(2))

    card_hit = None
    for cid, card in CARDS.items():
        if cid in low or card.name.lower() in low:
            card_hit = card
            break
    role_hit = None
    for alias, key in sorted(ROLE_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if alias in low:
            role_hit = key
            break
    amt = None
    ma = re.search(r"(\d+)\s*(монет|coins?)\b", low)
    if ma:
        amt = int(ma.group(1))

    out: dict = {"action": action, "code": code}
    if uses is not None:
        out["max_uses"] = uses
    if seconds is not None:
        out["duration_seconds"] = seconds
    if amt is not None:
        out["reward_type"] = "coins"
        out["reward_value"] = str(amt)
    elif card_hit:
        out["reward_type"] = "card"
        out["reward_value"] = card_hit.id
        out["reward_qty"] = 1
    elif role_hit:
        out["reward_type"] = "role"
        out["reward_value"] = role_hit
    return out


def parse_admin_local(text: str, author_id: int, bot_id: int) -> dict | None:
    raw = text.strip()
    raw_wo_bot = re.sub(rf"<@!?{bot_id}>", " ", raw).strip()
    low = raw_wo_bot.lower()

    promo = parse_promo_admin(low, raw_wo_bot)
    if promo:
        return promo

    everyone = bool(
        re.search(r"(@everyone|@here|\bвсем\b|\bвсему\b|\beveryone\b|\ball\b|\beverybody\b)", low)
    )
    mentions = [int(x) for x in MENTION_RE.findall(raw_wo_bot) if int(x) != bot_id]

    role_hit = role_key_from_mention(raw_wo_bot)
    if not role_hit:
        for alias, key in sorted(ROLE_ALIASES.items(), key=lambda kv: -len(kv[0])):
            if alias in low:
                role_hit = key
                break

    card_hit = None
    for cid, card in CARDS.items():
        if cid in low or card.name.lower() in low:
            card_hit = card
            break

    amount = None
    m_amt = re.search(r"(\d+)\s*(монет|coins?)\b", low)
    if m_amt:
        amount = int(m_amt.group(1))

    seconds = None
    m_for = re.search(r"(на|for)\s+(\d+\s*\w*)", low)
    if m_for:
        seconds = parse_duration(m_for.group(2))
    elif role_hit:
        seconds = parse_duration(low)

    uid = mentions[0] if mentions else (None if everyone else author_id)

    if role_hit and (everyone or mentions or "роль" in low or "role" in low or "выда" in low or "дай" in low):
        return {
            "action": "grant_role_all" if everyone or uid is None else "grant_role",
            "user_id": uid,
            "role_key": role_hit,
            "duration_seconds": seconds,
        }
    if card_hit and (everyone or "карт" in low or "card" in low or "выда" in low):
        return {
            "action": "give_card_all" if everyone else "give_card",
            "user_id": uid,
            "card_id": card_hit.id,
            "qty": 1,
        }
    if amount is not None:
        return {
            "action": "give_coins_all" if everyone else "give_coins",
            "user_id": uid or author_id,
            "amount": amount,
        }

    m = re.match(r"^/all\s+(coins?|монет\w*|role|роль|card|карт\w*)\s+(.+)$", raw_wo_bot, re.I)
    if m:
        kind, rest = m.group(1).lower(), m.group(2).strip()
        if kind.startswith("coin") or kind.startswith("монет"):
            num = re.search(r"-?\d+", rest)
            return {"action": "give_coins_all", "amount": int(num.group())} if num else None
        if kind.startswith("role") or kind.startswith("рол"):
            parts = rest.split()
            return {
                "action": "grant_role_all",
                "role_key": resolve_role_key(parts[0]) or parts[0],
                "duration_seconds": parse_duration(" ".join(parts[1:])) if len(parts) > 1 else None,
            }
        card = resolve_card(rest)
        if card:
            return {"action": "give_card_all", "card_id": card.id, "qty": 1}
    return None


def extract_action_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).removesuffix("```").strip()
    candidates = []
    try:
        candidates.append(json.loads(text))
    except json.JSONDecodeError:
        pass
    for m in re.finditer(r"\{[^{}]+\}", text):
        try:
            candidates.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            continue
    for obj in candidates:
        if isinstance(obj, list) and obj:
            obj = obj[0]
        if isinstance(obj, dict) and (obj.get("action") or obj.get("tool")):
            if obj.get("tool") and not obj.get("action"):
                obj["action"] = obj["tool"]
            return obj
    return None


def format_tool_result(result: dict) -> str:
    if not result.get("ok"):
        return f"❌ {result.get('error') or result}"
    parts = ["✅"]
    if "affected" in result:
        parts.append(f"выдано игрокам: **{result['affected']}**")
    if "role" in result:
        parts.append(f"роль `{result['role']}`")
    if result.get("expires_at"):
        parts.append(f"до <t:{int(result['expires_at'])}:R>")
    elif result.get("role"):
        parts.append("навсегда")
    if "amount" in result:
        parts.append(f"{result['amount']} 💰")
    if "card" in result:
        parts.append(f"карта `{result['card']}` ×{result.get('qty', 1)}")
    if "coins" in result and "amount" not in result:
        parts.append(f"баланс {result['coins']}")
    if result.get("failed"):
        parts.append(f"(ошибок: {result['failed']})")
    if result.get("promo"):
        parts.append(f"промо `{result['promo']}`")
    if result.get("promos_text"):
        return "✅ " + result["promos_text"]
    return " ".join(parts)


async def handle_admin_ai(message: discord.Message) -> None:
    text = (message.content or "").strip()
    bot_id = bot.user.id if bot.user else 0
    if not text:
        await message.reply(
            "Пустое сообщение (нужен Message Content Intent **или** пинг бота).\n"
            "Либо слэш: `/admin_all` → роль всем → `x2_coins` → `30`"
        )
        return

    local = parse_admin_local(text, message.author.id, bot_id)
    action = local or extract_action_json(text)
    if action:
        result = await run_admin_tool(message.guild, action.get("action") or "", action)
        await message.reply(format_tool_result(result))
        return

    if not CFG["ai"].get("api_key") or str(CFG["ai"]["api_key"]).startswith("PASTE"):
        await message.reply("Не понял. Примеры: `всем x2_coins на 30 сек` · `/admin_all`")
        return

    history = [{"role": "user", "content": f"Admin: {text}"}]
    async with message.channel.typing():
        try:
            data = await ai_admin.chat(CFG, history, use_tools=False)
        except Exception as exc:
            await message.reply(
                f"ИИ недоступен (`{exc}`). Сделай так:\n"
                "`всем x2_coins на 30 сек`\n`/admin_all` роль всем → x2_coins → 30"
            )
            return
        raw_out = (data["choices"][0]["message"].get("content") or "").strip()
        action = extract_action_json(raw_out)
        if action:
            result = await run_admin_tool(message.guild, action.get("action") or "", action)
            await message.reply(format_tool_result(result))
            return
        await message.reply((raw_out or "Не понял запрос.")[:1900])


async def run_admin_tool(guild: discord.Guild, name: str, args: dict) -> dict:
    uid = int(args.get("user_id") or 0)
    if name == "give_coins":
        if not uid:
            return {"ok": False, "error": "не указан игрок"}
        total = db.add_coins(uid, int(args["amount"]))
        return {"ok": True, "coins": total, "amount": int(args["amount"])}
    if name == "give_card":
        card = resolve_card(str(args["card_id"]))
        if not card:
            return {"ok": False, "error": "unknown card"}
        qty = db.add_card(uid, card.id, int(args.get("qty") or 1))
        return {"ok": True, "card": card.id, "qty": qty}
    if name == "inspect_user":
        data = db.get_user(uid) or {"coins": 0, "inventory": {}}
        return {"ok": True, **data}
    if name == "grant_role":
        key = resolve_role_key(str(args.get("role_key") or "")) or str(args.get("role_key") or "")
        duration = args.get("duration_seconds")
        herr = hierarchy_error(guild, key)
        if herr:
            return {"ok": False, "error": herr}
        if not uid:
            return {"ok": False, "error": "не указан игрок"}
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.HTTPException:
                return {"ok": False, "error": "member not found"}
        if member.bot:
            return {"ok": False, "error": "нельзя выдать роль боту"}
        rid = role_id(key) if key in CFG["roles"] else 0
        role = guild.get_role(rid) if rid else None
        if not role:
            return {"ok": False, "error": f"role {key} not configured"}
        try:
            await member.add_roles(role, reason="Tap Simulator admin")
        except discord.Forbidden:
            return {"ok": False, "error": hierarchy_error(guild, key) or "403 Forbidden — подними роль бота выше"}
        except discord.HTTPException as e:
            return {"ok": False, "error": str(e)}
        expires = time.time() + float(duration) if duration else None
        db.upsert_timed_role(uid, key, expires)
        return {"ok": True, "role": key, "expires_at": expires}
    if name == "revoke_role":
        key = resolve_role_key(str(args.get("role_key") or "")) or str(args.get("role_key") or "")
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.HTTPException:
                return {"ok": False, "error": "member not found"}
        rid = role_id(key) if key in CFG["roles"] else 0
        role = guild.get_role(rid) if rid else None
        if role:
            try:
                await member.remove_roles(role, reason="Tap Simulator admin")
            except discord.HTTPException as e:
                return {"ok": False, "error": str(e)}
        db.delete_timed_role(uid, key)
        return {"ok": True}
    if name in ("give_coins_all", "give_card_all", "grant_role_all", "revoke_role_all"):
        return await run_admin_all(guild, name, args)
    if name == "create_promo":
        return admin_create_promo(args)
    if name == "update_promo":
        return admin_update_promo(args)
    if name == "disable_promo":
        ok, msg = db.update_promo(str(args.get("code") or ""), active=0)
        return {"ok": ok, "error": None if ok else msg, "promo": msg if ok else None}
    if name == "list_promos":
        rows = db.list_promos()
        if not rows:
            return {"ok": True, "promos_text": "промокодов нет"}
        lines = []
        for p in rows:
            cap = p["max_uses"] or "∞"
            flag = "on" if p["active"] else "off"
            extra = f" {p['duration_seconds']}s" if p["reward_type"] == "role" and p.get("duration_seconds") else ""
            lines.append(
                f"`{p['code']}` [{flag}] {p['reward_type']}={p['reward_value']} ×{p['reward_qty']}{extra} · {p['uses']}/{cap}"
            )
        return {"ok": True, "promos_text": "\n".join(lines)[:1800]}
    if name in ("craft_custom", "create_craft", "add_craft"):
        ings = args.get("ingredients") or args.get("ings") or []
        if isinstance(ings, str):
            ings = crafts.parse_ings(ings) or []
        else:
            parsed = []
            for x in ings:
                c = resolve_card(str(x))
                if c:
                    parsed.append(c.id)
            ings = parsed
        if not ings or len(set(ings)) < 3:
            return {"ok": False, "error": "нужно ≥3 разных карты"}
        out = resolve_card(str(args.get("result") or args.get("card_id") or ""))
        if not out:
            return {"ok": False, "error": "неизвестный результат"}
        if out.id in UNIQUE_IDS:
            return {"ok": False, "error": "Crown Tap нельзя крафтить"}
        ok, why = crafts.recipe_legal(ings, out.id)
        if not ok:
            return {"ok": False, "error": why}
        tid = args.get("target_user_id") or args.get("user_id")
        tid = int(tid) if tid else None
        rid = db.craft_custom_add(
            tag=str(args.get("tag") or "custom"),
            ings=ings,
            coins=int(args.get("coins") or 0),
            result=out.id,
            target_user_id=tid,
            created_by=int(args.get("created_by") or 0) or None,
        )
        return {"ok": True, "promo": f"craft#{rid}", "card": out.id}
    if name in ("craft_list", "list_crafts"):
        rows = db.craft_custom_list(include_off=True)
        if not rows:
            return {"ok": True, "promos_text": "кастом-рецептов нет"}
        lines = []
        for r in rows[:20]:
            who = f"user {r['target_user_id']}" if r["target_user_id"] else "все"
            lines.append(f"#{r['id']} [{r['active']}] {r['tag']} {who} → {r['result']}")
        return {"ok": True, "promos_text": "\n".join(lines)}
    if name in ("craft_off", "disable_craft"):
        ok = db.craft_custom_off(int(args.get("recipe_id") or args.get("id") or 0))
        return {"ok": ok, "error": None if ok else "нет рецепта"}
    return {"ok": False, "error": "unknown tool"}


async def run_admin_all(guild: discord.Guild, name: str, args: dict) -> dict:
    if name == "grant_role_all":
        key = resolve_role_key(str(args.get("role_key") or "")) or str(args.get("role_key") or "")
        herr = hierarchy_error(guild, key)
        if herr:
            return {"ok": False, "error": herr}
        args = {**args, "role_key": key}
    ok_n = 0
    fail = 0
    last_err = ""
    async for member in iter_targets(guild):
        one = dict(args)
        one["user_id"] = member.id
        inner = name.removesuffix("_all")
        last = await run_admin_tool(guild, inner, one)
        if last.get("ok"):
            ok_n += 1
        else:
            fail += 1
            last_err = str(last.get("error") or "")
        await asyncio.sleep(0.3)
    if ok_n == 0:
        return {
            "ok": False,
            "error": last_err
            or "никого не нашёл. Включи SERVER MEMBERS INTENT и подними роль бота выше X2 COINS",
        }
    out: dict = {"ok": True, "affected": ok_n, "failed": fail}
    if name == "give_coins_all":
        out["amount"] = int(args.get("amount") or 0)
    if name == "give_card_all":
        out["card"] = args.get("card_id")
        out["qty"] = int(args.get("qty") or 1)
    if name == "grant_role_all":
        out["role"] = args.get("role_key")
        dur = args.get("duration_seconds")
        out["expires_at"] = time.time() + float(dur) if dur else None
    return out


def admin_create_promo(args: dict) -> dict:
    rtype = str(args.get("reward_type") or "")
    rval = str(args.get("reward_value") or args.get("amount") or args.get("card_id") or args.get("role_key") or "")
    if rtype == "coins" and not rval and args.get("amount") is not None:
        rval = str(args["amount"])
    if rtype == "card":
        card = resolve_card(rval)
        if not card:
            return {"ok": False, "error": "неизвестная карта"}
        rval = card.id
    if rtype == "role":
        key = resolve_role_key(rval) or rval
        if key not in CFG["roles"]:
            return {"ok": False, "error": f"роль {rval} неизвестна"}
        rval = key
    ok, msg = db.create_promo(
        str(args.get("code") or ""),
        reward_type=rtype,
        reward_value=rval,
        reward_qty=int(args.get("reward_qty") or args.get("qty") or 1),
        max_uses=int(args.get("max_uses") or 0),
        duration_seconds=args.get("duration_seconds"),
        created_by=args.get("user_id"),
    )
    return {"ok": ok, "error": None if ok else msg, "promo": msg if ok else None}


def admin_update_promo(args: dict) -> dict:
    fields = {}
    if args.get("max_uses") is not None:
        fields["max_uses"] = int(args["max_uses"])
    if args.get("reward_type"):
        fields["reward_type"] = args["reward_type"]
    if args.get("reward_value") is not None:
        fields["reward_value"] = str(args["reward_value"])
    if args.get("reward_qty") is not None:
        fields["reward_qty"] = int(args["reward_qty"])
    if "duration_seconds" in args:
        fields["duration_seconds"] = args.get("duration_seconds")
    if args.get("active") is not None:
        fields["active"] = int(args["active"])
    ok, msg = db.update_promo(str(args.get("code") or ""), **fields)
    return {"ok": ok, "error": None if ok else msg, "promo": msg if ok else None}


async def apply_promo_reward(guild: discord.Guild, member: discord.Member, promo: dict) -> str:
    kind = promo["reward_type"]
    if kind == "coins":
        amt = int(promo["reward_value"])
        total = db.add_coins(member.id, amt)
        return f"+{amt} 💰 (баланс {total})"
    if kind == "card":
        card = resolve_card(str(promo["reward_value"]))
        if not card:
            return "карта не найдена"
        qty = db.add_card(member.id, card.id, int(promo["reward_qty"] or 1))
        return f"{format_card(card)} (×{qty})"
    if kind == "role":
        key = str(promo["reward_value"])
        res = await run_admin_tool(
            guild,
            "grant_role",
            {
                "user_id": member.id,
                "role_key": key,
                "duration_seconds": promo.get("duration_seconds"),
            },
        )
        if not res.get("ok"):
            return f"роль не выдалась: {res.get('error')}"
        return f"роль `{key}`"
    return "неизвестная награда"



async def handle_shards(message: discord.Message, member: discord.Member) -> None:
    haste = db.upgrade_level(member.id, "haste")
    cd_s = max(0.8, float(CFG["game"]["tap_cooldown_seconds"]) - 0.3 * haste)
    if bot.on_cd(member.id, "shard", cd_s) > 0:
        return
    await ensure_player_role(member)
    ok, text = upd3.farm_shard(member.id, tap_gain_for(member))
    try:
        await message.add_reaction("💎" if ok else "❌")
    except discord.HTTPException:
        pass
    await message.reply(text, mention_author=False, delete_after=30)


async def handle_crystal_ex(message: discord.Message, member: discord.Member) -> None:
    if bot.on_cd(member.id, "crystal_ex", 3) > 0:
        return
    await ensure_player_role(member)
    ok, text = upd3.exchange_crystal(member.id)
    await message.reply(text, mention_author=False, delete_after=30)


async def handle_ref_message(message: discord.Message, member: discord.Member) -> None:
    text = (message.content or "").strip()
    if text.lower() in ("/ref", "ref"):
        code = db.ref_code_for(member.id)
        await message.reply(
            f"Твой код: `{code}` · приглашено {db.ref_stats(member.id)}\nНапиши этот код в этот канал с нового аккаунта.",
            mention_author=False,
        )
        return
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", text):
        return
    ok, msg = upd3.apply_ref(member.id, text)
    await message.reply(("✅ " if ok else "❌ ") + msg, mention_author=False, delete_after=30)


async def handle_promo_message(message: discord.Message, member: discord.Member) -> None:
    text = (message.content or "").strip()
    if not text or any(c.isspace() for c in text):
        return
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", text):
        return
    ok, reason, promo = db.redeem_promo(text, member.id)
    if not ok:
        msg = {
            "not_found": "❌ такого промокода нет",
            "already": "❌ ты уже активировал этот промокод",
            "exhausted": "❌ активации закончились",
        }.get(reason, "❌ не вышло")
        await message.reply(msg, mention_author=False)
        return
    await ensure_player_role(member)
    reward = await apply_promo_reward(message.guild, member, promo or {})
    left = ""
    if promo and promo["max_uses"]:
        left = f" · осталось {max(0, promo['max_uses'] - promo['uses'] - 1)}/{promo['max_uses']}"
    await message.reply(f"✅ Промокод `{db.normalize_code(text)}` · {reward}{left}", mention_author=False)


async def resolve_player(guild: discord.Guild, user_id: int, cached_name: str | None = None):
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            member = None
    if member:
        remember(member)
    return member, (member.display_name if member else (cached_name or f"игрок {user_id}"))


async def refresh_leaderboard() -> None:
    channel = bot.get_channel(ch("leaderboard"))
    if not isinstance(channel, discord.TextChannel):
        return
    rows = db.leaderboard(int(CFG["game"]["leaderboard_size"]))
    guild = channel.guild
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    top_id = rows[0]["user_id"] if rows else None
    top_avatar = None
    for i, row in enumerate(rows, start=1):
        medal = medals[i - 1] if i <= 3 else f"`#{i}`"
        member, name = await resolve_player(guild, row["user_id"], row.get("display_name"))
        if i == 1:
            if member:
                top_avatar = member.display_avatar.url
            elif row.get("avatar_url"):
                top_avatar = row["avatar_url"]
        lines.append(f"{medal} **{name}** — **{row['coins']}** 💰")
    desc = "\n".join(lines) if lines else "Пока пусто. Пишите в tap-канал."
    embed = discord.Embed(title="🏆 Tap Simulator — Leaderboard", description=desc, color=0xF1C40F)
    embed.set_footer(text="Обновляется каждый час · ник и монеты")
    if top_avatar:
        embed.set_thumbnail(url=top_avatar)

    mid = db.get_meta("leaderboard_message_id")
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
        db.set_meta("leaderboard_message_id", msg.id)

    # TOP-1 role
    rid = role_id("top1")
    role = guild.get_role(rid) if rid else None
    if role and top_id:
        for member in list(role.members):
            if member.id != top_id:
                try:
                    await member.remove_roles(role, reason="No longer #1")
                except discord.HTTPException:
                    pass
        champ = guild.get_member(top_id) if top_id else None
        if champ is None and top_id:
            try:
                champ = await guild.fetch_member(top_id)
            except discord.HTTPException:
                champ = None
        if champ and role and role not in champ.roles:
            try:
                await champ.add_roles(role, reason="Leaderboard #1")
            except discord.HTTPException:
                pass
        if champ:
            remember(champ)
            awarded = db.award_top1_card(champ.id, champ.display_name)
            if awarded:
                card = CARDS[CROWN_CARD_ID]
                try:
                    e = discord.Embed(
                        title="⚜️ Crown Tap",
                        description=(
                            f"{champ.mention} впервые стал TOP-1 и получил {card.emoji} **{card.name}**\n"
                            f"`#{awarded['serial']}` · **{awarded['inscribed_name']}**\n"
                            f"Надпись вечная. Суперспособность, пока карта у тебя: **x2 coins + x2 luck**. Повторно не выдаётся."
                        ),
                        color=0xF1C40F,
                    )
                    f = card_file(CROWN_CARD_ID)
                    if f:
                        e.set_image(url=f"attachment://{CROWN_CARD_ID}.jpg")
                        await channel.send(embed=e, file=f, delete_after=30)
                    else:
                        await channel.send(embed=e, delete_after=30)
                except discord.HTTPException:
                    pass


# ---------- slash / prefix helpers ----------

class InventoryView(discord.ui.View):
    def __init__(self, owner_id: int, inventory: dict[str, int]):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        options = []
        for cid, qty in inventory.items():
            card = CARDS.get(cid)
            if not card:
                continue
            options.append(
                discord.SelectOption(
                    label=f"{card.name} ×{qty}"[:100],
                    value=cid,
                    emoji=card.emoji,
                    description=card.rarity.upper(),
                )
            )
        if options:
            self.add_item(CardSelect(options[:25]))


class CardSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(placeholder="Открыть карточку…", options=options)

    async def callback(self, interaction: discord.Interaction):
        view: InventoryView = self.view  # type: ignore
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message("Это не твой инвентарь.", ephemeral=True)
            return
        cid = self.values[0]
        card = CARDS[cid]
        e = discord.Embed(
            title=f"{card.emoji} {card.name}",
            description=format_card(card),
            color=0x9B59B6 if card.rarity == "rare" else 0x3498DB,
        )
        f = card_file(cid)
        if f:
            e.set_image(url=f"attachment://{cid}.jpg")
            await interaction.response.send_message(embed=e, file=f, ephemeral=True)
        else:
            await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="profile", description="Монеты и карточки")
async def slash_profile(interaction: discord.Interaction, user: discord.User | None = None):
    target = user or interaction.user
    if isinstance(target, discord.Member):
        remember(target)
    data = db.get_user(target.id)
    embed = profile_embed(target, data)
    featured = None
    inv = (data or {}).get("inventory") or {}
    for cid in list(EXCLUSIVE_IDS) + list(RARE_IDS) + list(COMMON_IDS):
        if inv.get(cid):
            featured = cid
            break
    f = card_file(featured) if featured else None
    if f:
        await interaction.response.send_message(embed=embed, file=f, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="inventory", description="Инвентарь с картинками карт")
async def slash_inventory(interaction: discord.Interaction):
    data = db.get_user(interaction.user.id) or {"inventory": {}}
    inv = data.get("inventory") or {}
    if not inv:
        await interaction.response.send_message("Инвентарь пуст — крути карты в roll-канале.", ephemeral=True)
        return
    e = discord.Embed(title=f"Инвентарь — {interaction.user.display_name}", color=0x8E44AD)
    for cid, qty in inv.items():
        card = CARDS.get(cid)
        if card:
            e.add_field(name=f"{card.emoji} {card.name}", value=f"×{qty} · {card.rarity}", inline=True)
    first = next(iter(inv))
    f = card_file(first)
    if f:
        e.set_image(url=f"attachment://{first}.jpg")
    view = InventoryView(interaction.user.id, inv)
    kwargs = {"embed": e, "view": view, "ephemeral": True}
    if f:
        kwargs["file"] = f
    await interaction.response.send_message(**kwargs)


@bot.tree.command(name="daily", description="Ежедневная награда")
async def slash_daily(interaction: discord.Interaction):
    if isinstance(interaction.user, discord.Member):
        remember(interaction.user)
        await ensure_player_role(interaction.user)
    ok, streak, pay = db.claim_daily(interaction.user.id, date.today().isoformat())
    if not ok:
        await interaction.response.send_message(
            f"Уже забирал сегодня. Серия: **{streak}** дн.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"✅ Daily **+{pay}** 💰 · серия **{streak}** день"
    )


@bot.tree.command(name="cards", description="Список карточек")
async def slash_cards(interaction: discord.Interaction):
    commons = "\n".join(format_card(CARDS[i]) for i in COMMON_IDS)
    rares = "\n".join(format_card(CARDS[i]) for i in RARE_IDS)
    e = discord.Embed(title="Карточки Tap Simulator", color=0x8E44AD)
    e.add_field(name="Common (без удачи)", value=commons, inline=False)
    e.add_field(name="Rare (нужна удача / апгрейд luck)", value=rares, inline=False)
    e.add_field(
        name="Exclusive",
        value="\n".join(format_card(CARDS[i]) for i in EXCLUSIVE_IDS),
        inline=False,
    )
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="give", description="Передать монеты или карточку (лучше через reply в trade-канале)")
@app_commands.describe(target="Кому", item="50 или golden_tap или neon_tap x2")
async def slash_give(interaction: discord.Interaction, target: discord.Member, item: str):
    if interaction.channel_id != ch("trade") and ch("trade"):
        await interaction.response.send_message(
            f"Трейд только в <#{ch('trade')}> (или reply `/give …` там).",
            ephemeral=True,
        )
        return
    if target.id == interaction.user.id:
        await interaction.response.send_message("Себе нельзя.", ephemeral=True)
        return
    ok, text = do_give(interaction.user.id, target.id, item)
    await interaction.response.send_message(text)


@bot.tree.command(name="sell", description="Продать common-карту за монеты (макс. 50 за штуку)")
@app_commands.describe(item="wooden_tap или crystal_tap 2")
async def slash_sell(interaction: discord.Interaction, item: str):
    ok, text = do_sell(interaction.user.id, item)
    await interaction.response.send_message(text)



@bot.tree.command(name="enchant", description="Зачаровать карту (монеты или удача)")
async def slash_enchant(interaction: discord.Interaction):
    err = require_channel(interaction, "enchant")
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return
    if isinstance(interaction.user, discord.Member):
        await ensure_player_role(interaction.user)
    await interaction.response.send_message(
        embed=upd3.enchant_embed(interaction.user.id),
        view=upd3.EnchantView(interaction.user.id),
        ephemeral=True,
    )


@bot.tree.command(name="brawl", description="[ADMIN] Драка в click")
@app_commands.describe(minutes="длительность, по умолчанию 2")
async def slash_brawl(interaction: discord.Interaction, minutes: float = 2.0):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("Только ADMIN.", ephemeral=True)
        return
    if upd3.brawl_active():
        await interaction.response.send_message("Драка уже идёт.", ephemeral=True)
        return
    tap = interaction.guild.get_channel(ch("tap")) if interaction.guild else None
    if interaction.channel_id != ch("tap"):
        await interaction.response.send_message("Только в click.", ephemeral=True)
        return
    end = upd3.start_brawl(minutes)
    await interaction.response.send_message(
        f"⚔️ **Драка!** {minutes} мин. Reply на сообщение игрока = удар на размер вашего тапа. До <t:{int(end)}:R>"
    )


@bot.tree.command(name="ref", description="Реферальный код")
async def slash_ref(interaction: discord.Interaction):
    err = require_channel(interaction, "ref")
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return
    code = db.ref_code_for(interaction.user.id)
    await interaction.response.send_message(
        f"Код: `{code}` · засчитано приглашений: **{db.ref_stats(interaction.user.id)}**\n"
        f"Новый игрок пишет код в этот канал, затем тапает в click.",
        ephemeral=True,
    )


@bot.tree.command(name="shardshop", description="Магазин осколков")
async def slash_shardshop(interaction: discord.Interaction):
    err = require_channel(interaction, "shards")
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return
    await interaction.response.send_message(
        embed=upd3.shard_embed(interaction.user.id),
        view=upd3.ShardShop(interaction.user.id),
        ephemeral=True,
    )


@bot.tree.command(name="upgrade", description="Купить улучшения за монеты")
async def slash_upgrade(interaction: discord.Interaction):
    err = require_channel(interaction, "upgrade")
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return
    if isinstance(interaction.user, discord.Member):
        await ensure_player_role(interaction.user)
        remember(interaction.user)
    await interaction.response.send_message(
        embed=upgrades.embed_for(interaction.user.id),
        view=upgrades.UpgradeView(interaction.user.id),
        ephemeral=True,
    )


@bot.tree.command(name="crafts", description="Верстак: 4 рецепта, смена каждые 30 мин")
async def slash_crafts(interaction: discord.Interaction):
    err = require_channel(interaction, "craft")
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return
    if isinstance(interaction.user, discord.Member):
        await ensure_player_role(interaction.user)
        remember(interaction.user)
    await interaction.response.send_message(
        embed=crafts.embed_for(interaction.user.id),
        view=crafts.CraftView(interaction.user.id),
        ephemeral=True,
    )


@bot.tree.command(name="craft_custom", description="[ADMIN] кастом-рецепт всем или одному")
@app_commands.describe(
    ingredients="3+ карты через запятую: wooden_tap, copper_tap, steel_tap",
    result="что получится",
    coins="стоимость монет",
    tag="название рецепта",
    target="пусто = для всех игроков",
)
async def slash_craft_custom(
    interaction: discord.Interaction,
    ingredients: str,
    result: str,
    coins: int,
    tag: str = "custom",
    target: discord.Member | None = None,
):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("Только ADMIN.", ephemeral=True)
        return
    ings = crafts.parse_ings(ingredients)
    if not ings:
        await interaction.response.send_message("Нужно ≥3 **разных** известных карты через запятую.", ephemeral=True)
        return
    out = resolve_card(result)
    if not out:
        await interaction.response.send_message("Неизвестная карта-результат.", ephemeral=True)
        return
    if out.id in UNIQUE_IDS:
        await interaction.response.send_message("Crown Tap нельзя сделать результатом крафта.", ephemeral=True)
        return
    ok, why = crafts.recipe_legal(ings, out.id)
    if not ok:
        await interaction.response.send_message(f"❌ {why}", ephemeral=True)
        return
    if coins < 0:
        await interaction.response.send_message("Монеты ≥ 0.", ephemeral=True)
        return
    rid = db.craft_custom_add(
        tag=tag,
        ings=ings,
        coins=coins,
        result=out.id,
        target_user_id=target.id if target else None,
        created_by=interaction.user.id,
    )
    who = target.mention if target else "**всем**"
    line = " + ".join(f"{CARDS[i].emoji} {CARDS[i].name}" for i in ings)
    await interaction.response.send_message(
        f"✅ Рецепт `#{rid}` {who}: {line} + {coins}💰 → {format_card(out)}\n"
        f"Ротация 4 слотов общая. Этот слот — кастом."
    )


@bot.tree.command(name="craft_list", description="[ADMIN] список кастом-рецептов")
async def slash_craft_list(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("Только ADMIN.", ephemeral=True)
        return
    rows = db.craft_custom_list(include_off=True)
    if not rows:
        await interaction.response.send_message("Кастом-рецептов нет.", ephemeral=True)
        return
    lines = []
    for r in rows[:25]:
        ings = ", ".join(json.loads(r["ings"]))
        flag = "on" if r["active"] else "off"
        who = f"<@{r['target_user_id']}>" if r["target_user_id"] else "все"
        lines.append(f"`#{r['id']}` [{flag}] **{r['tag']}** {who}: {ings} → `{r['result']}` ({r['coins']}💰)")
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


@bot.tree.command(name="craft_off", description="[ADMIN] выключить кастом-рецепт")
async def slash_craft_off(interaction: discord.Interaction, recipe_id: int):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("Только ADMIN.", ephemeral=True)
        return
    ok = db.craft_custom_off(recipe_id)
    await interaction.response.send_message("✅ выключен" if ok else "❌ нет такого id", ephemeral=True)


@bot.tree.command(name="duel", description="Дуэль на монеты или карту — 3 раунда TAP")
@app_commands.describe(rival="Противник", stake="50 или golden_tap или wooden_tap 2")
async def slash_duel(interaction: discord.Interaction, rival: discord.Member, stake: str):
    duel_ch = ch("duel")
    if not duel_ch:
        await interaction.response.send_message(
            f"Канал **{channel_style('duel')}** ещё не привязан: в `config.json` стоит прочерк `"
            f"channels.duel`. Создай канал `⚔️・duel` и вставь его ID вместо `—`.",
            ephemeral=True,
        )
        return
    if interaction.channel_id != duel_ch:
        await interaction.response.send_message(
            f"Дуэли только в <#{duel_ch}> (`{channel_style('duel')}`).",
            ephemeral=True,
        )
        return
    await duels.start_challenge(interaction, rival, stake)


@bot.tree.command(name="admin_give", description="[ADMIN] выдать монеты/карту")
@app_commands.describe(target="Игрок", item="100 или legend_tap x1")
async def slash_admin_give(interaction: discord.Interaction, target: discord.Member, item: str):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("Только ADMIN.", ephemeral=True)
        return
    cm = COINS_RE.match(item.strip())
    if cm:
        total = db.add_coins(target.id, int(cm.group(1)))
        await interaction.response.send_message(f"Выдано {cm.group(1)} 💰 → {target.mention} (баланс {total})")
        return
    card = resolve_card(item)
    if not card:
        await interaction.response.send_message("Неизвестный предмет.", ephemeral=True)
        return
    qty = db.add_card(target.id, card.id, 1)
    await interaction.response.send_message(f"Выдана {format_card(card)} (×{qty}) → {target.mention}")


@bot.tree.command(name="event_role", description="[ADMIN] выдать ивент-роль навсегда или на N секунд")
@app_commands.describe(role="x5_coins / x2_coins / x5_luck", seconds="пусто = навсегда")
@app_commands.choices(
    role=[
        app_commands.Choice(name="X5 COINS", value="x5_coins"),
        app_commands.Choice(name="X2 COINS", value="x2_coins"),
        app_commands.Choice(name="X5 LUCK", value="x5_luck"),
        app_commands.Choice(name="X2 LUCK", value="x2_luck"),
    ]
)
async def slash_event_role(
    interaction: discord.Interaction,
    target: discord.Member,
    role: app_commands.Choice[str],
    seconds: int | None = None,
):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("Только ADMIN.", ephemeral=True)
        return
    result = await run_admin_tool(
        interaction.guild,
        "grant_role",
        {"user_id": target.id, "role_key": role.value, "duration_seconds": seconds},
    )
    await interaction.response.send_message(f"`{result}`")


@bot.tree.command(name="admin_all", description="[ADMIN] выдать всем монеты / роль / карту")
@app_commands.describe(what="coins / role / card", value="100 или x5_luck или golden_tap", seconds="для роли, пусто = навсегда")
@app_commands.choices(
    what=[
        app_commands.Choice(name="монеты всем", value="coins"),
        app_commands.Choice(name="роль всем", value="role"),
        app_commands.Choice(name="карта всем", value="card"),
    ]
)
async def slash_admin_all(
    interaction: discord.Interaction,
    what: app_commands.Choice[str],
    value: str,
    seconds: int | None = None,
):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("Только ADMIN.", ephemeral=True)
        return
    await interaction.response.defer()
    if what.value == "coins":
        amt = int(re.search(r"-?\d+", value).group())
        result = await run_admin_tool(interaction.guild, "give_coins_all", {"amount": amt})
    elif what.value == "role":
        key = resolve_role_key(value) or value
        result = await run_admin_tool(
            interaction.guild,
            "grant_role_all",
            {"role_key": key, "duration_seconds": seconds},
        )
    else:
        card = resolve_card(value)
        if not card:
            await interaction.followup.send("Неизвестная карта.")
            return
        result = await run_admin_tool(interaction.guild, "give_card_all", {"card_id": card.id, "qty": 1})
    await interaction.followup.send(format_tool_result(result))


@bot.tree.command(name="promo_create", description="[ADMIN] создать промокод")
@app_commands.describe(
    code="Код без пробелов",
    reward="coins / card / role",
    value="100 или golden_tap или x5_luck",
    uses="0 = безлимит",
    seconds="для роли",
)
@app_commands.choices(
    reward=[
        app_commands.Choice(name="монеты", value="coins"),
        app_commands.Choice(name="карта", value="card"),
        app_commands.Choice(name="роль", value="role"),
    ]
)
async def slash_promo_create(
    interaction: discord.Interaction,
    code: str,
    reward: app_commands.Choice[str],
    value: str,
    uses: int = 0,
    seconds: int | None = None,
):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("Только ADMIN.", ephemeral=True)
        return
    result = admin_create_promo(
        {
            "code": code,
            "reward_type": reward.value,
            "reward_value": value,
            "max_uses": uses,
            "duration_seconds": seconds,
            "user_id": interaction.user.id,
        }
    )
    await interaction.response.send_message(format_tool_result(result))


@bot.tree.command(name="promo_list", description="[ADMIN] список промокодов")
async def slash_promo_list(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("Только ADMIN.", ephemeral=True)
        return
    await interaction.response.send_message(format_tool_result(await run_admin_tool(interaction.guild, "list_promos", {})), ephemeral=True)


@bot.tree.command(name="promo_off", description="[ADMIN] выключить промокод")
async def slash_promo_off(interaction: discord.Interaction, code: str):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("Только ADMIN.", ephemeral=True)
        return
    await interaction.response.send_message(
        format_tool_result(await run_admin_tool(interaction.guild, "disable_promo", {"code": code}))
    )


@bot.tree.command(name="boss", description="Статус серверного босса")
async def slash_boss(interaction: discord.Interaction):
    if bosses.is_alive():
        await interaction.response.send_message(embed=bosses.embed_for(), ephemeral=True)
        return
    s = db.get_meta("boss") or {}
    left = int(s.get("next_at") or 0) - int(time.time())
    if left > 0:
        await interaction.response.send_message(f"Босса нет. Следующий через ~{left // 60} мин.", ephemeral=True)
    else:
        await interaction.response.send_message("Босса нет, сейчас должен заспавниться в click.", ephemeral=True)


@bot.tree.command(name="boss_spawn", description="[ADMIN] призвать босса")
@app_commands.choices(
    tier=[
        app_commands.Choice(name="Common 500HP", value="common"),
        app_commands.Choice(name="Uncommon 1500HP", value="uncommon"),
        app_commands.Choice(name="Rare 2500HP + Titan Tap", value="rare"),
    ]
)
async def slash_boss_spawn(interaction: discord.Interaction, tier: app_commands.Choice[str]):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("Только ADMIN.", ephemeral=True)
        return
    channel = interaction.guild.get_channel(ch("tap")) if interaction.guild else None
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("Нет tap-канала.", ephemeral=True)
        return
    bosses.start_boss(tier.value)
    await bosses.refresh_message(channel, force=True)
    await interaction.response.send_message(f"Босс `{tier.value}` в <#{channel.id}>")


@bot.tree.command(name="refresh_lb", description="[ADMIN] обновить лидерборд сейчас")
async def slash_refresh_lb(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("Только ADMIN.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await refresh_leaderboard()
    await interaction.followup.send("Лидерборд обновлён.", ephemeral=True)


def main() -> None:
    token = CFG.get("discord_token") or ""
    if not token or token.startswith("PASTE"):
        raise SystemExit("Put discord_token into config.json")
    print("intents bitmask", bot.intents.value)
    bot.run(token)


if __name__ == "__main__":
    main()
