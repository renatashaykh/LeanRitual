# 🥗 NutriBot — AI Nutrition Tracker

A personal Telegram bot that uses **Claude Vision** to analyse meal photos and text, track daily macros, and give personalised feedback — configured for a fat-loss goal.

---

## ✨ Features

- 📸 **Photo analysis** — send a meal photo and get instant macro estimates
- ✍️ **Text logging** — describe a meal in words if no photo available
- 📊 **Daily running totals** — tracks calories, protein, carbs, and fat across the day
- 🎯 **Personalised targets** — training day vs rest day calorie/protein cycling
- 💡 **Contextual feedback** — Claude sees your remaining budget and gives smart meal suggestions
- 🔄 **Daily reset** — one tap to clear the log and start fresh

---

## 🚀 Quick Start (Local)

### 1. Prerequisites

- Python 3.12+
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- An Anthropic API key (from [console.anthropic.com](https://console.anthropic.com))

### 2. Clone and install

```bash
git clone https://github.com/you/nutribot.git
cd nutribot
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in your tokens
```

### 4. Run

```bash
python bot.py
```

Open Telegram, find your bot, and send `/start`.

---

## 🎯 Your Targets (pre-configured)

| Day type | Calories | Protein | Carbs | Fat |
|----------|----------|---------|-------|-----|
| 🏋️ Training | 1,700–1,750 kcal | 115–120g | 160–175g | 52–58g |
| 😴 Rest | 1,600–1,650 kcal | 108–112g | 140–155g | 50–56g |

Default is **Training Day**. Switch with the keyboard buttons.

---

## 📱 How to Use

| Action | How |
|--------|-----|
| Log a meal (photo) | Send any photo; add a caption for extra context |
| Log a meal (text) | Type "I had 150g chicken with rice and salad" |
| Check daily total | Tap **📊 Daily Summary** |
| Switch to training day | Tap **🏋️ Training Day** |
| Switch to rest day | Tap **😴 Rest Day** |
| Reset the day | Tap **🔄 Reset Today** |
| Summary via command | `/summary` |
| Reset via command | `/reset` |

---

## ☁️ Deployment

### Option A — Railway (recommended, easiest)

1. Push your code to a GitHub repo
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
3. Select your repo — Railway detects the `Dockerfile` automatically
4. Go to **Variables** and add:
   ```
   TELEGRAM_BOT_TOKEN=...
   ANTHROPIC_API_KEY=...
   ALLOWED_USER_IDS=your_telegram_id
   USER_TIMEZONE=Europe/London
   ```
5. Click **Deploy** — done! The bot runs 24/7.

> 💡 **Find your Telegram ID**: message [@userinfobot](https://t.me/userinfobot)

> 💡 **Persistent storage on Railway**: add a Volume in the service settings, mount it at `/data`, and set `DATA_DIR=/data` in your env vars.

---

### Option B — Render

1. Push to GitHub
2. Go to [render.com](https://render.com) → **New** → **Blueprint** → connect your repo
3. Render reads `render.yaml` automatically and creates a **Worker** service with a 1 GB disk
4. Set the secret env vars in the Render dashboard:
   ```
   TELEGRAM_BOT_TOKEN=...
   ANTHROPIC_API_KEY=...
   ALLOWED_USER_IDS=your_telegram_id
   ```
5. Click **Apply** — Render builds and starts the bot

> ⚠️ Render's free tier **spins down** workers after inactivity. Use the **Starter plan ($7/mo)** for a persistent worker.

---

### Option C — VPS / any server

```bash
# On your server
git clone https://github.com/you/nutribot.git && cd nutribot
cp .env.example .env && nano .env

# Run with Docker
docker build -t nutribot .
docker run -d --env-file .env -v $(pwd)/data:/app/data --name nutribot nutribot

# Or with systemd (no Docker)
pip install -r requirements.txt
# Create /etc/systemd/system/nutribot.service (see below)
```

Systemd unit file:
```ini
[Unit]
Description=NutriBot Telegram Bot
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/nutribot
EnvironmentFile=/home/ubuntu/nutribot/.env
ExecStart=/home/ubuntu/nutribot/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## 🔧 Customisation

### Change your targets
Edit `targets.py` — adjust `kcal_min/max`, `protein_min/max`, etc.

### Change your profile
Edit `PROFILE` in `targets.py` for age, weight, height, goal.

### Change the AI model
Edit `ANTHROPIC_MODEL` in `bot.py`. Currently uses `claude-opus-4-5`.

### Add persistent storage (SQLite)
Replace the JSON functions in `storage.py` with SQLite calls using Python's built-in `sqlite3`. The `DailyLog` dataclass stays the same.

---

## 📁 Project Structure

```
nutribot/
├── bot.py          # Telegram bot, handlers, Claude API calls
├── storage.py      # Daily log persistence (JSON files)
├── targets.py      # Personalised macro targets + profile
├── prompts.py      # Claude prompt templates
├── requirements.txt
├── Dockerfile
├── railway.toml    # Railway config
├── render.yaml     # Render config
├── .env.example
└── .gitignore
```

---

## 🔒 Security

- Set `ALLOWED_USER_IDS` to your Telegram user ID so only you can use the bot
- Never commit `.env` to git (it's in `.gitignore`)
- Your API keys are only ever in environment variables, never in code
