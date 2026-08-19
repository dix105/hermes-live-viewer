# Hermes live viewer

Local operator UI for a Hermes Agent home (`~/.hermes`).

Not the official `hermes serve` dashboard. This reads the same SQLite / gateway files Hermes already writes and projects them as:

- **Chats** (`/live`) — last 12h Telegram/CLI sessions, real topic names, WhatsApp-style list, tools, markdown, branches vs subagents, typing ring, session token footer
- **Cron** (`/cron`) — jobs + last run output
- **Status** (`/status`) — gateway/platforms, daily + model usage charts, last gateway errors
- **Skills** (`/`) — catalog of `~/.hermes/skills`

## Run

```bash
python3 live_server.py
# http://127.0.0.1:8471/live
```

Binds `0.0.0.0:8471`. Put it on Tailscale (or similar) if you want it off-localhost. Do not expose it to the public internet — there is no auth.

## What it reads

- `~/.hermes/state.db` — sessions, messages, token usage
- `~/.hermes/gateway_state.json` — platform health
- `~/.hermes/cron/jobs.json` + `cron/output/`
- `~/.hermes/logs/gateway.log` — error slice only
- Telegram topic titles via `messages.getForumTopicsByID` (bot token + Telethon). Session titles are not used as topic names.

Secrets, bot tokens, process command lines, and Telethon session files are not served.

## Honest limits

- Token charts use **session totals** attributed to last activity day. Hermes does not store per-day increments.
- Per-reply token counts are not stored (`messages.token_count` is empty).
- Official token-by-token streaming is `hermes serve` `/api/events`. This viewer polls SQLite.
