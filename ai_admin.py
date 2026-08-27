import json
from typing import Any
from urllib.parse import urlparse

import aiohttp

SYSTEM = """You are the ADMIN helper for Tap Simulator (Discord). Reply in the admin's language, short and exact.

GAME (facts — do not invent):
- Channels: click=taps+boss, roll=cards, promos=one-word codes, duel=/duel, trade=/give reply, board=leaderboard, upgrades=/upgrade, crafts=/crafts, admin=this chat.
- First play grants PLAYER.
- Tap: coins. Roles X2/X5 COINS multiply. Collection bonus. Combo +5 / 10 taps in 10s. Upgrades: power, luck, boss dmg, haste.
- Roll: no luck = 5 commons only. X2/X5 LUCK or luck-upgrade unlock rares. Titan Tap and Crown Tap NEVER drop from roll.
- Sell: only 5 commons, max 50 coins (wooden 10, copper 18, steel 28, golden 40, crystal 50).
- Duel: ⚔️ only. Stake 5–5000 coins or ≤5 of one non-exclusive card. Bo3 TAP, winner ×2.
- Boss in click: Common 500 / Uncommon 1500 / Rare 2500 HP. Loot by damage. Rare: Titan Tap to ONE random hitter. Cooldown ~35 min.
- Crown Tap: first time a player becomes TOP-1. Serial + nickname forever (even after trade). Holding it = x2 coins AND x2 luck (max with roles). Never awarded twice to same user. Unique, not craftable.
- Crafts: 4 rotating recipes identical for ALL players, change every 30 min, need ≥3 different cards + coins. Admin can add custom recipes for everyone (target_user_id null) or one user.
- Promo: one code per player once. Persist redemptions.

If the admin wants a CHANGE, output ONLY one JSON object, no markdown, no extra text:
{"action":"give_coins","user_id":1,"amount":10}
{"action":"give_card","user_id":1,"card_id":"golden_tap","qty":1}
{"action":"grant_role","user_id":1,"role_key":"x5_luck","duration_seconds":3600}
{"action":"revoke_role","user_id":1,"role_key":"x2_coins"}
{"action":"give_coins_all","amount":10}
{"action":"grant_role_all","role_key":"x2_coins","duration_seconds":600}
{"action":"give_card_all","card_id":"wooden_tap","qty":1}
{"action":"inspect_user","user_id":1}
{"action":"create_promo","code":"TAP100","reward_type":"coins","reward_value":"100","max_uses":50}
{"action":"create_promo","code":"LUCK","reward_type":"role","reward_value":"x5_luck","duration_seconds":3600,"max_uses":20}
{"action":"create_promo","code":"GOLD","reward_type":"card","reward_value":"golden_tap","reward_qty":1,"max_uses":10}
{"action":"update_promo","code":"TAP100","max_uses":80}
{"action":"disable_promo","code":"TAP100"}
{"action":"list_promos"}
{"action":"craft_custom","ingredients":["wooden_tap","copper_tap","steel_tap"],"result":"neon_tap","coins":200,"tag":"ивент","target_user_id":null}
{"action":"craft_list"}
{"action":"craft_off","recipe_id":1}

role_key enum: x5_coins, x2_coins, x5_luck, x2_luck, player, top1.
duration_seconds null = forever. Everyone = give_*_all / grant_role_all, never invent a fake user_id.
Do NOT output JSON if they only asked a question — then answer in plain text using the facts above.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "give_coins",
            "description": "Add coins to a user (negative to take).",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer"},
                    "amount": {"type": "integer"},
                },
                "required": ["user_id", "amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "give_card",
            "description": "Give a card to a user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer"},
                    "card_id": {"type": "string"},
                    "qty": {"type": "integer", "default": 1},
                },
                "required": ["user_id", "card_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grant_role",
            "description": "Grant event/admin role. duration_seconds null = forever.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer"},
                    "role_key": {
                        "type": "string",
                        "enum": ["x5_coins", "x2_coins", "x5_luck", "x2_luck", "player", "top1"],
                    },
                    "duration_seconds": {"type": ["integer", "null"]},
                },
                "required": ["user_id", "role_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "revoke_role",
            "description": "Remove an event role from a user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer"},
                    "role_key": {"type": "string"},
                },
                "required": ["user_id", "role_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_user",
            "description": "Look up coins, taps, inventory.",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "integer"}},
                "required": ["user_id"],
            },
        },
    },
]


def _client_headers(base_url: str, extra: dict | None) -> dict[str, str]:
    host = (urlparse(base_url).hostname or "").lower()
    headers: dict[str, str] = {}
    # Custom gateways (AgentRouter etc.) whitelist known CLI clients.
    if "agentrouter" in host:
        headers.update(
            {
                "User-Agent": "codex_cli_rs/0.101.0 (Mac OS 26.0.1; arm64) Apple_Terminal/464",
                "Originator": "codex_cli_rs",
                "Version": "0.101.0",
                "HTTP-Referer": "https://github.com/openai/codex",
                "X-Title": "Codex",
            }
        )
    if extra:
        headers.update({str(k): str(v) for k, v in extra.items()})
    return headers


async def chat(cfg: dict, messages: list[dict[str, Any]], *, use_tools: bool = True) -> dict[str, Any]:
    ai = cfg["ai"]
    url = ai["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {ai['api_key']}",
        "Content-Type": "application/json",
        **_client_headers(ai["base_url"], ai.get("extra_headers")),
    }
    if ai.get("api_key"):
        headers.setdefault("x-api-key", ai["api_key"])

    payload: dict[str, Any] = {
        "model": ai["model"],
        "messages": [{"role": "system", "content": SYSTEM}, *messages],
    }
    if use_tools and ai.get("use_tools", True):
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"

    timeout = aiohttp.ClientTimeout(total=int(ai.get("timeout", 90)))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            text = await resp.text()
            if text.lstrip().startswith("<!") or "aliyun_waf" in text:
                raise RuntimeError(
                    "AI gateway returned a WAF/captcha page. "
                    "Try another network or set ai.extra_headers in config.json."
                )
            if resp.status >= 400:
                # Custom models often reject the tools field — retry once without it.
                if use_tools and resp.status in (400, 404, 422):
                    return await chat(cfg, messages, use_tools=False)
                raise RuntimeError(f"AI HTTP {resp.status}: {text[:500]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"AI returned non-JSON: {text[:300]}") from exc
