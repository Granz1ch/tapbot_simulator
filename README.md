# Tap Simulator

Discord — размещение. Бот — вся экономика. SQLite лежит рядом с ботом: `data/tap_simulator.db`.

## Что делает

| Канал | Поведение |
| --- | --- |
| **tap** | Любое сообщение = монеты. Без роли `×1`. Роль **X2 COINS** → `×2`. Роль **X5 COINS** → `×5`. |
| **spin** | Любое сообщение = прокрутка карты. Без **X5 LUCK** падают только 5 common. С ролью в пул входят 5 rare (удача ×5). |
| **trade** | Reply на сообщение игрока: `/give 50` или `/give golden_tap` или `/give neon_tap x2`. |
| **admin_ai** | Только роль **ADMIN**. Ассистент с tool-calling: монеты, карты, временные роли. Ключ ИИ в конфиге. |
| **leaderboard** | Раз в час правит **одно** сообщение. Если удалили — шлёт заново. #1 получает роль **TOP-1**. |

Роли: `ADMIN`, `TOP-1`, `PLAYER`, ивенты `X5 COINS`, `X2 COINS`, `X5 LUCK`.  
Админ выдаёт ивент навсегда или на N секунд (`/event_role` или через ИИ).

Карты:

- Common: Wooden, Copper, Steel, Golden, Crystal  
- Rare (нужен X5 LUCK): Neon, Void, Phoenix, Aurora, Legend  

## Установка

1. Создай приложение на [Discord Developer Portal](https://discord.com/developers/applications):
   - Bot → Reset Token
   - Privileged intents: **MESSAGE CONTENT**, **SERVER MEMBERS**
   - OAuth2 URL: `bot` + `applications.commands`  
     права: Send Messages, Read History, Add Reactions, Manage Roles, Manage Messages, Embed Links
2. На сервере создай роли и 5 каналов. Роль бота **выше** выдаваемых ролей.
3. Канал `admin_ai`: права только у ADMIN + бот (View + Send). Остальным запрети.
4. На машине с ботом:

```bash
cd tap-simulator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
# заполни токены и все ID
python bot.py
```

ID канала: ПКМ по каналу → Copy Channel ID (включи Developer Mode).  
То же для ролей и сервера.

## config.json

```json
{
  "discord_token": "...",
  "guild_id": 123,
  "channels": { "tap": 0, "spin": 0, "trade": 0, "admin_ai": 0, "leaderboard": 0 },
  "roles": {
    "admin": 0, "top1": 0, "player": 0,
    "x5_coins": 0, "x2_coins": 0, "x5_luck": 0
  },
  "ai": {
    "api_key": "sk-...",
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini"
  }
}
```

`base_url` может быть любым OpenAI-compatible (OpenAI, Groq, Together, локальный LM Studio).

## Команды

- `/profile` `/cards`
- `/give` (или текст `/give …` ответом в trade)
- `/admin_give` `/event_role` `/refresh_lb` — только ADMIN

ИИ в admin-канале понимает, например:  
«выдай <@id> 500 монет и X5 LUCK на 3600 секунд и карту legend_tap».
