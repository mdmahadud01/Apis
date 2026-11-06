# Quotex Candle API + Telegram Controller (Railway)

A Railway-ready service based on the supplied `API.py`, `server.py`, and `pyquotex-1.0.3.tar.gz`.

## Repository files

Keep all these files in the repository root (do not put them in a subfolder):

- `app.py` — Telegram bot, Quotex connection, candle collector, Flask API
- `pyquotex-1.0.3.tar.gz` — supplied library source archive (kept in the repository)
- `requirements.txt`
- `Dockerfile`
- `railway.json`
- `Procfile`

The Docker build extracts the supplied archive and installs the package source directly. The archive itself does not contain `setup.py`/`pyproject.toml`, so `pip install ./pyquotex-1.0.3.tar.gz` is not a valid install method. The build also installs NumPy explicitly because `pyquotex.utils.indicators` imports it although the supplied package metadata does not declare it. This avoids the common `indicators.py` import failure.

## Deploy to Railway

1. Upload/push these root files to a GitHub repository.
2. In Railway, create a project from that GitHub repository.
3. Add variable `BOT_TOKEN` with your Telegram BotFather token. Do not commit the token to GitHub.
4. Optional: set `USE_DEMO=true` (default) or `USE_DEMO=false`.
5. Add a Railway Volume mounted at `/data`. This is important: Railway's normal container filesystem is ephemeral. Without a persistent volume, SQLite candle history can be lost on redeploy/restart.
6. Deploy. Railway exposes the Flask API on the assigned `PORT`.

## Telegram commands

- `/start` — asks for Quotex SSID token, then CSRF token. Use `/skipcsrf` if you do not have a CSRF value.
- `/logout` — closes the Quotex session; candle/payout history remains in SQLite.
- `/server` — starts a Cloudflare Quick Tunnel and returns its temporary `https://....trycloudflare.com` URL.
- `/status` — shows connection and stored-data counts.
- `/cancel` — cancels an in-progress token prompt.

The first Telegram account to send `/start` claims bot ownership; the owner chat ID is stored in SQLite. Keep the bot private and protect the Railway volume. Credentials are not written to the database. Telegram messages are not an ideal secret channel; use a private chat and delete credential messages after login.

## API endpoints

- `GET /?Pair=USDPHP_otc&Candle=5000&Time=1m`
- `GET /markets`
- `GET /latest?Pair=USDPHP_otc&Time=1m`
- `GET /payout-history?Pair=USDPHP_otc&Time=1m`
- `GET /health`

## Important limitations

- Candle history is persisted in SQLite and survives `/logout` when `/data` is a persistent Railway Volume.
- On a later login the collector requests recent and historical candle windows and upserts by timestamp, so returned history fills missing records where the Quotex endpoint makes them available. A complete gap-fill cannot be guaranteed: provider history limits, market availability, expired sessions, and API changes can prevent retrieval.
- `trycloudflare.com` Quick Tunnel URLs are temporary and may change after the process restarts. Railway's generated public domain is preferable for a stable API URL; `/server` is provided because it was explicitly requested.
- This project uses an unofficial third-party Quotex client. Provider-side changes may break connectivity. Use demo mode unless you understand the risks.
