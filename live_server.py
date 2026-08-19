#!/usr/bin/env python3
"""Static skills viewer + live session chat API."""

from __future__ import annotations

import json
import sqlite3
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = Path("/root/.hermes/state.db")
LIVE_WINDOW_SEC = 180
LIST_WINDOW_SEC = 12 * 3600
MSG_LIMIT = 80


def db() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), timeout=8)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def parse_tool_calls(raw) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else {}
        args = item.get("arguments") or item.get("args") or item.get("input") or fn.get("arguments") or {}
        if not isinstance(args, str):
            try:
                args = json.dumps(args, ensure_ascii=False, indent=2)
            except Exception:
                args = str(args)
        out.append(
            {
                "id": item.get("call_id") or item.get("id"),
                "name": item.get("name") or item.get("tool_name") or fn.get("name") or "tool",
                "input": args[:12000],
            }
        )
    return out


def tool_output(content: str) -> str:
    text = content or ""
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "output" in data:
            text = data["output"] if isinstance(data["output"], str) else json.dumps(data["output"], ensure_ascii=False, indent=2)
        elif not isinstance(data, str):
            text = json.dumps(data, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return text[:2500]


def preview(text: str, n: int = 90) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text[:n]



def _origin(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


_TOPIC_CACHE_PATH = ROOT / "telegram-topics.json"
_topic_cache: dict[tuple[str, str], str] = {}
_topic_lock = None


def _topic_lock_get():
    global _topic_lock
    import threading
    if _topic_lock is None:
        _topic_lock = threading.Lock()
    return _topic_lock


def _load_topic_cache() -> None:
    if _topic_cache:
        return
    if not _TOPIC_CACHE_PATH.exists():
        return
    try:
        data = json.loads(_TOPIC_CACHE_PATH.read_text())
        for k, name in (data or {}).items():
            if "|" in k and name:
                chat_id, thread_id = k.split("|", 1)
                _topic_cache[(str(chat_id), str(thread_id))] = str(name)
    except Exception:
        pass


def _save_topic_cache() -> None:
    payload = {f"{c}|{t}": n for (c, t), n in _topic_cache.items() if n}
    _TOPIC_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _telegram_creds():
    env = {}
    for path in ("/root/.openclaw/workspace/support-automation/.env", "/root/.hermes/.env"):
        pth = Path(path)
        if not pth.exists():
            continue
        for line in pth.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    token = env.get("TELEGRAM_BOT_TOKEN")
    api_id = env.get("TELEGRAM_API_ID")
    api_hash = env.get("TELEGRAM_API_HASH")
    if not (token and api_id and api_hash):
        return None
    return int(api_id), api_hash, token


def _fetch_telegram_topic_names(pairs: list[tuple[str, str]]) -> None:
    """Real source: messages.getForumTopicsByID (Telethon / MTProto)."""
    creds = _telegram_creds()
    if not creds or not pairs:
        return
    api_id, api_hash, token = creds
    by_chat: dict[str, list[int]] = {}
    for chat_id, thread_id in pairs:
        try:
            by_chat.setdefault(str(chat_id), []).append(int(thread_id))
        except Exception:
            continue
    if not by_chat:
        return

    import asyncio
    from telethon import TelegramClient
    from telethon.tl.functions.messages import GetForumTopicsByIDRequest

    async def _run():
        client = TelegramClient(str(ROOT / ".telethon-bot"), api_id, api_hash)
        await client.start(bot_token=token)
        try:
            for chat_id, ids in by_chat.items():
                peer = int(chat_id)
                # Telegram accepts a batch per chat
                unique = list(dict.fromkeys(ids))
                try:
                    res = await client(GetForumTopicsByIDRequest(peer=peer, topics=unique))
                except Exception:
                    continue
                for topic in getattr(res, "topics", None) or []:
                    tid = getattr(topic, "id", None)
                    name = getattr(topic, "title", None) or getattr(topic, "name", None)
                    if tid is not None:
                        _topic_cache[(str(chat_id), str(tid))] = str(name or "")
        finally:
            await client.disconnect()

    asyncio.run(_run())
    _save_topic_cache()


def resolve_telegram_topics(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Cache only on the request path. Telegram fetch runs in a background thread."""
    _load_topic_cache()
    need = []
    for pair in pairs:
        key = (str(pair[0]), str(pair[1]))
        if key[0] and key[1] and key not in _topic_cache:
            need.append(key)
    if need:
        import threading
        def _bg(keys=list(need)):
            with _topic_lock_get():
                still = [k for k in keys if k not in _topic_cache]
                if not still:
                    return
                try:
                    _fetch_telegram_topic_names(still)
                except Exception:
                    for k in still:
                        _topic_cache.setdefault(k, "")
                    _save_topic_cache()
        threading.Thread(target=_bg, daemon=True).start()
    return {k: _topic_cache[k] for k in ((str(a), str(b)) for a, b in pairs) if k in _topic_cache and _topic_cache[k]}


def _topic_for(con: sqlite3.Connection, row, resolved: dict) -> tuple[str, str]:
    origin = _origin(row["origin_json"] if "origin_json" in row.keys() else None)
    chat_name = origin.get("chat_name") or row["display_name"] or ""
    thread_id = row["thread_id"] or origin.get("thread_id")
    chat_id = row["chat_id"] or origin.get("chat_id")
    topic = (origin.get("chat_topic") or "").strip()
    if not topic and chat_id and thread_id:
        topic = resolved.get((str(chat_id), str(thread_id)), "")
    return topic, chat_name


def list_live_sessions() -> list:
    con = db()
    try:
        rows = con.execute(
            """
            SELECT s.id, s.source, s.model, s.started_at, s.title,
                   s.session_key, s.chat_id, s.chat_type, s.display_name,
                   s.thread_id, s.origin_json,
                   s.message_count, s.tool_call_count,
                   MAX(m.timestamp) AS last_ts,
                   MAX(m.id) AS last_msg_id,
                   (
                     SELECT COUNT(*) FROM sessions c
                     WHERE c.parent_session_id = s.id
                   ) AS child_count
            FROM sessions s
            JOIN messages m ON m.session_id = s.id
            WHERE COALESCE(s.archived, 0) = 0
              AND (s.source IS NULL OR s.source != 'subagent')
            GROUP BY s.id
            HAVING last_ts >= ?
            ORDER BY last_ts DESC
            LIMIT 40
            """,
            (time.time() - LIST_WINDOW_SEC,),
        ).fetchall()
        pairs = []
        for r in rows:
            origin = _origin(r["origin_json"])
            chat_id = r["chat_id"] or origin.get("chat_id")
            thread_id = r["thread_id"] or origin.get("thread_id")
            if r["source"] == "telegram" and chat_id and thread_id:
                pairs.append((str(chat_id), str(thread_id)))
        resolved = resolve_telegram_topics(pairs)

        out = []
        for r in rows:
            last = con.execute(
                """
                SELECT role, content, tool_name, tool_calls, timestamp FROM messages
                WHERE session_id = ? ORDER BY id DESC LIMIT 1
                """,
                (r["id"],),
            ).fetchone()
            snippet = ""
            typing = False
            if last:
                age = time.time() - float(last["timestamp"] or r["last_ts"] or 0)
                inflight = last["role"] in ("user", "tool") or (
                    last["role"] == "assistant" and bool(parse_tool_calls(last["tool_calls"]))
                )
                typing = bool(inflight and age <= LIVE_WINDOW_SEC)
                if typing:
                    snippet = "typing…"
                elif last["role"] == "tool":
                    snippet = f"{last['tool_name'] or 'tool'} finished"
                else:
                    snippet = preview(last["content"] or "")
            topic, chat_name = _topic_for(con, r, resolved)
            if r["source"] == "telegram" and topic:
                title = topic
                subtitle = chat_name or "telegram"
            else:
                title = chat_name or r["display_name"] or r["session_key"] or r["id"]
                subtitle = r["source"] or ""
            out.append(
                {
                    "id": r["id"],
                    "source": r["source"],
                    "model": r["model"],
                    "title": title,
                    "subtitle": subtitle,
                    "topic": topic,
                    "chat_name": chat_name,
                    "thread_id": r["thread_id"],
                    "session_key": r["session_key"],
                    "chat_type": r["chat_type"],
                    "message_count": r["message_count"],
                    "child_count": r["child_count"] or 0,
                    "last_ts": r["last_ts"],
                    "last_msg_id": r["last_msg_id"],
                    "preview": snippet,
                    "typing": typing,
                    "live": bool(r["last_ts"] and (time.time() - float(r["last_ts"])) <= LIVE_WINDOW_SEC),
                }
            )
        return out
    finally:
        con.close()


def list_children(parent_id: str) -> list:
    con = db()
    try:
        rows = con.execute(
            """
            SELECT s.id, s.model, s.started_at, s.ended_at, s.source,
                   s.title, s.message_count, s.tool_call_count,
                   MAX(m.timestamp) AS last_ts
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.parent_session_id = ?
            GROUP BY s.id
            ORDER BY COALESCE(s.ended_at, 9999999999) ASC, last_ts DESC
            """,
            (parent_id,),
        ).fetchall()
        now = time.time()
        out = []
        for r in rows:
            goal = con.execute(
                """
                SELECT content FROM messages
                WHERE session_id = ? AND role = 'user'
                ORDER BY id ASC LIMIT 1
                """,
                (r["id"],),
            ).fetchone()
            last = con.execute(
                """
                SELECT role, content, tool_name FROM messages
                WHERE session_id = ? ORDER BY id DESC LIMIT 1
                """,
                (r["id"],),
            ).fetchone()
            last_ts = r["last_ts"] or r["started_at"] or 0
            running = (r["ended_at"] is None) and last_ts and (now - float(last_ts) <= LIVE_WINDOW_SEC)
            snippet = ""
            if last:
                snippet = preview(last["content"] or "") if last["role"] != "tool" else f"{last['tool_name'] or 'tool'} finished"
            kind = "subagent" if (r["source"] or "") == "subagent" else "branch"
            if kind == "subagent":
                title = preview(goal["content"] if goal else "", 72) or r["id"]
            else:
                title = (r["title"] or "").strip() or preview(goal["content"] if goal else "", 72) or "branch"
            out.append(
                {
                    "id": r["id"],
                    "model": r["model"],
                    "kind": kind,
                    "title": title,
                    "message_count": r["message_count"] or 0,
                    "last_ts": last_ts,
                    "ended_at": r["ended_at"],
                    "preview": snippet,
                    "live": bool(running),
                    "status": "running" if running else ("done" if r["ended_at"] else "idle"),
                }
            )
        return out
    finally:
        con.close()



def session_usage_rows(con, session_id: str) -> list:
    rows = con.execute(
        """
        SELECT model, billing_provider, api_call_count, input_tokens, output_tokens,
               cache_read_tokens, reasoning_tokens, last_seen
        FROM session_model_usage
        WHERE session_id = ?
        ORDER BY last_seen DESC
        """,
        (session_id,),
    ).fetchall()
    return [{
        "model": r["model"],
        "provider": r["billing_provider"],
        "calls": r["api_call_count"] or 0,
        "input_tokens": r["input_tokens"] or 0,
        "output_tokens": r["output_tokens"] or 0,
        "cache_read_tokens": r["cache_read_tokens"] or 0,
        "reasoning_tokens": r["reasoning_tokens"] or 0,
        "last_seen": r["last_seen"],
    } for r in rows]


def build_chat(session_id: str, after: int = 0, limit: int = MSG_LIMIT) -> dict:
    con = db()
    try:
        lookback = after
        if after:
            has_tools = con.execute(
                """
                SELECT 1 FROM messages
                WHERE session_id = ? AND id > ? AND role = 'tool'
                LIMIT 1
                """,
                (session_id, after),
            ).fetchone()
            if has_tools:
                lookback = max(0, after - 40)
        if after:
            rows = con.execute(
                """
                SELECT id, role, content, tool_call_id, tool_calls, tool_name,
                       timestamp, reasoning, reasoning_content
                FROM messages
                WHERE session_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (session_id, lookback, limit + (40 if lookback != after else 0)),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT id, role, content, tool_call_id, tool_calls, tool_name,
                       timestamp, reasoning, reasoning_content
                FROM (
                    SELECT id, role, content, tool_call_id, tool_calls, tool_name,
                           timestamp, reasoning, reasoning_content
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (session_id, limit),
            ).fetchall()

        items = []
        pending = {}
        last_id = after
        running = []

        for r in rows:
            last_id = r["id"]
            role = r["role"]
            content = r["content"] or ""
            if isinstance(content, bytes):
                content = content.decode("utf-8", "replace")
            thinking = r["reasoning_content"] or r["reasoning"] or ""
            if isinstance(thinking, bytes):
                thinking = thinking.decode("utf-8", "replace")

            if role == "user":
                items.append({"id": r["id"], "kind": "user", "text": content, "timestamp": r["timestamp"]})
                continue

            if role == "assistant":
                tools = parse_tool_calls(r["tool_calls"])
                item = {
                    "id": r["id"],
                    "kind": "assistant",
                    "text": content,
                    "thinking": thinking,
                    "tools": [],
                    "timestamp": r["timestamp"],
                }
                for t in tools:
                    card = {
                        "id": t["id"],
                        "name": t["name"],
                        "input": t["input"],
                        "output": "",
                        "status": "running",
                    }
                    item["tools"].append(card)
                    if t["id"]:
                        pending[t["id"]] = card
                items.append(item)
                continue

            if role == "tool":
                cid = r["tool_call_id"]
                output = tool_output(content)
                if cid and cid in pending:
                    pending[cid]["output"] = output
                    pending[cid]["status"] = "done"
                    if not pending[cid]["name"] and r["tool_name"]:
                        pending[cid]["name"] = r["tool_name"]
                else:
                    items.append(
                        {
                            "id": r["id"],
                            "kind": "assistant",
                            "text": "",
                            "thinking": "",
                            "tools": [
                                {
                                    "id": cid,
                                    "name": r["tool_name"] or "tool",
                                    "input": "",
                                    "output": output,
                                    "status": "done",
                                }
                            ],
                            "timestamp": r["timestamp"],
                        }
                    )

        for card in pending.values():
            if card["status"] == "running":
                running.append({"id": card["id"], "name": card["name"]})

        meta = con.execute(
            "SELECT id, source, parent_session_id, display_name, title, session_key, model FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        info = dict(meta) if meta else {"id": session_id}
        return {"items": items, "running_tools": running, "last_id": last_id, "session": info, "usage": session_usage_rows(con, session_id)}
    finally:
        con.close()


CRON_JOBS = Path("/root/.hermes/cron/jobs.json")
CRON_OUT = Path("/root/.hermes/cron/output")


def list_cron() -> list:
    if not CRON_JOBS.exists():
        return []
    try:
        data = json.loads(CRON_JOBS.read_text())
    except Exception:
        return []
    jobs = data.get("jobs") or []
    out = []
    for job in jobs:
        jid = job.get("id") or ""
        last_file = None
        last_preview = ""
        d = CRON_OUT / jid
        if d.is_dir():
            files = sorted(d.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)
            if files:
                last_file = files[0].name
                try:
                    last_preview = files[0].read_text(errors="replace")[:280].strip()
                except Exception:
                    last_preview = ""
        sched = job.get("schedule_display") or ""
        if not sched and isinstance(job.get("schedule"), dict):
            sched = job["schedule"].get("display") or job["schedule"].get("expr") or ""
        out.append({
            "id": jid,
            "name": job.get("name") or jid,
            "schedule": sched,
            "enabled": bool(job.get("enabled")),
            "state": job.get("state") or "",
            "next_run_at": job.get("next_run_at"),
            "last_run_at": job.get("last_run_at"),
            "last_status": job.get("last_status"),
            "last_error": job.get("last_error"),
            "last_file": last_file,
            "preview": last_preview,
        })
    out.sort(key=lambda x: x.get("next_run_at") or "", reverse=False)
    return out


def cron_detail(job_id: str) -> dict:
    jobs = {j["id"]: j for j in list_cron()}
    job = jobs.get(job_id)
    if not job:
        return {"error": "not found"}
    output = ""
    path = None
    d = CRON_OUT / job_id
    if d.is_dir():
        files = sorted(d.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)
        if files:
            path = files[0].name
            output = files[0].read_text(errors="replace")[:20000]
    job = dict(job)
    job["output"] = output
    job["output_file"] = path
    return job



GATEWAY_STATE = Path("/root/.hermes/gateway_state.json")
PROCESSES = Path("/root/.hermes/processes.json")
GATEWAY_LOG = Path("/root/.hermes/logs/gateway.log")


def _redact(text: str) -> str:
    import re
    s = str(text or "")
    s = re.sub(r"(?i)(api[_-]?key|token|secret|password|bearer)\s*[:=]\s*\S+", r"\1=***", s)
    s = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-***", s)
    s = re.sub(r"https?://[^:@\s]+:[^@\s]+@", "https://***:***@", s)
    return s



def usage_stats(days: int = 30) -> dict:
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    con = db()
    try:
        rows = con.execute(
            """
            SELECT u.session_id, u.model, u.billing_provider, u.api_call_count,
                   u.input_tokens, u.output_tokens, u.cache_read_tokens,
                   u.reasoning_tokens, u.last_seen, u.first_seen,
                   s.display_name, s.title, s.source
            FROM session_model_usage u
            LEFT JOIN sessions s ON s.id = u.session_id
            ORDER BY u.last_seen DESC
            """
        ).fetchall()
    finally:
        con.close()

    now = datetime.now(ist)
    day_keys = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days-1, -1, -1)]
    by_day = {k: {"date": k, "input": 0, "output": 0, "cache": 0, "think": 0, "calls": 0} for k in day_keys}
    by_model = {}
    chats = []
    for r in rows:
        ts = r["last_seen"] or r["first_seen"]
        day = ""
        if ts:
            day = datetime.fromtimestamp(float(ts), tz=ist).strftime("%Y-%m-%d")
        inp = r["input_tokens"] or 0
        out = r["output_tokens"] or 0
        cache = r["cache_read_tokens"] or 0
        think = r["reasoning_tokens"] or 0
        calls = r["api_call_count"] or 0
        model = r["model"] or "unknown"
        if day in by_day:
            by_day[day]["input"] += inp
            by_day[day]["output"] += out
            by_day[day]["cache"] += cache
            by_day[day]["think"] += think
            by_day[day]["calls"] += calls
        m = by_model.setdefault(model, {"model": model, "input": 0, "output": 0, "cache": 0, "think": 0, "calls": 0, "sessions": 0})
        m["input"] += inp
        m["output"] += out
        m["cache"] += cache
        m["think"] += think
        m["calls"] += calls
        m["sessions"] += 1
        chats.append({
            "session_id": r["session_id"],
            "model": model,
            "provider": r["billing_provider"],
            "calls": calls,
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": cache,
            "reasoning_tokens": think,
            "last_seen": r["last_seen"],
            "day": day,
            "title": r["title"] or r["display_name"] or r["session_id"],
            "source": r["source"],
        })
    models = sorted(by_model.values(), key=lambda x: x["input"] + x["output"], reverse=True)
    return {
        "days": [by_day[k] for k in day_keys],
        "models": models,
        "chats": chats[:80],
        "timezone": "IST",
        "note": "Daily bars use last activity day. Hermes stores session totals, not per-day increments.",
    }


def list_usage(limit: int = 20) -> list:
    con = db()
    try:
        rows = con.execute(
            """
            SELECT u.session_id, u.model, u.billing_provider, u.api_call_count,
                   u.input_tokens, u.output_tokens, u.cache_read_tokens,
                   u.reasoning_tokens, u.estimated_cost_usd, u.actual_cost_usd,
                   u.cost_status, u.last_seen,
                   s.display_name, s.title, s.source, s.thread_id
            FROM session_model_usage u
            LEFT JOIN sessions s ON s.id = u.session_id
            ORDER BY u.last_seen DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "session_id": r["session_id"],
                "model": r["model"],
                "provider": r["billing_provider"],
                "calls": r["api_call_count"] or 0,
                "input_tokens": r["input_tokens"] or 0,
                "output_tokens": r["output_tokens"] or 0,
                "cache_read_tokens": r["cache_read_tokens"] or 0,
                "reasoning_tokens": r["reasoning_tokens"] or 0,
                "cost_usd": r["actual_cost_usd"] or r["estimated_cost_usd"] or 0,
                "cost_status": r["cost_status"],
                "last_seen": r["last_seen"],
                "title": r["title"] or r["display_name"] or r["session_id"],
                "source": r["source"],
            })
        return out
    finally:
        con.close()


def gateway_errors(limit: int = 40) -> list:
    if not GATEWAY_LOG.exists():
        return []
    keys = (" ERROR ", "ERROR", "FATAL", "Traceback", "failed to connect", "CRITICAL", "✗ ")
    hits = []
    try:
        raw = GATEWAY_LOG.read_bytes()
    except Exception:
        return []
    # walk backwards in chunks until we have enough hits
    pos = len(raw)
    chunk = 220000
    while pos > 0 and len(hits) < limit:
        start = max(0, pos - chunk)
        data = raw[start:pos].decode("utf-8", "replace")
        found = []
        for line in data.splitlines():
            if any(k in line for k in keys):
                found.append(_redact(line.strip())[:400])
        hits = found + hits
        pos = start
        if start == 0:
            break
    return hits[-limit:]


def gateway_status() -> dict:
    gs = {}
    if GATEWAY_STATE.exists():
        try:
            gs = json.loads(GATEWAY_STATE.read_text())
        except Exception:
            gs = {}
    platforms = gs.get("platforms") or {}
    clean_platforms = {}
    broken = []
    for name, info in platforms.items():
        state = (info or {}).get("state") or "unknown"
        clean_platforms[name] = {
            "state": state,
            "error_code": (info or {}).get("error_code"),
            "error_message": _redact((info or {}).get("error_message") or ""),
            "updated_at": (info or {}).get("updated_at"),
        }
        if state not in ("connected", "running", "ready", "ok"):
            broken.append(name)
    procs = []
    if PROCESSES.exists():
        try:
            raw = json.loads(PROCESSES.read_text())
            if isinstance(raw, list):
                for p in raw[:20]:
                    procs.append({
                        "pid": p.get("pid"),
                        "session_key": p.get("session_key"),
                        "started_at": p.get("started_at"),
                        "task_id": p.get("task_id"),
                    })
        except Exception:
            pass
    return {
        "ok": not broken and (gs.get("gateway_state") == "running"),
        "broken": broken,
        "gateway_state": gs.get("gateway_state"),
        "pid": gs.get("pid"),
        "kind": gs.get("kind"),
        "active_agents": gs.get("active_agents"),
        "updated_at": gs.get("updated_at"),
        "start_time": gs.get("start_time"),
        "exit_reason": gs.get("exit_reason"),
        "restart_requested": gs.get("restart_requested"),
        "platforms": clean_platforms,
        "processes": procs,
        "errors": gateway_errors(),
        "usage": list_usage(15),
    }



class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/"):
            return
        super().log_message(fmt, *args)

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/live/status":
            try:
                self._json(gateway_status())
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        if path == "/api/live/usage":
            try:
                self._json(usage_stats(30))
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        if path == "/api/live/cron":
            try:
                self._json({"jobs": list_cron(), "now": time.time()})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        if path.startswith("/api/live/cron/"):
            jid = path[len("/api/live/cron/"):]
            try:
                payload = cron_detail(jid)
                self._json(payload, 404 if payload.get("error") else 200)
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        if path == "/api/live/sessions":
            try:
                self._json({"sessions": list_live_sessions(), "now": time.time()})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        if path.startswith("/api/live/sessions/") and path.endswith("/children"):
            sid = path[len("/api/live/sessions/") : -len("/children")]
            try:
                self._json({"children": list_children(sid)})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        if path.startswith("/api/live/sessions/") and path.endswith("/messages"):
            sid = path[len("/api/live/sessions/") : -len("/messages")]
            after = int(qs.get("after", ["0"])[0] or 0)
            try:
                self._json(build_chat(sid, after=after))
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        if path == "/api/live/stream":
            sid = (qs.get("session") or [""])[0]
            after = int(qs.get("after", ["0"])[0] or 0)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                last = after
                idle = 0
                while idle < 150:
                    payload = build_chat(sid, after=last, limit=80)
                    items = payload["items"]
                    if items:
                        last = payload["last_id"]
                        idle = 0
                    else:
                        idle += 1
                    frame = json.dumps(payload, ensure_ascii=False, default=str)
                    self.wfile.write(f"data: {frame}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(0.7)
            except (BrokenPipeError, ConnectionResetError):
                return
            return

        if path == "/live":
            self.path = "/live.html"
        if path == "/cron":
            self.path = "/cron.html"
        if path == "/status":
            self.path = "/status.html"
        return super().do_GET()


def main():
    server = ThreadingHTTPServer(("0.0.0.0", 8471), Handler)
    print("skills+live http://0.0.0.0:8471/  chat=/live", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
