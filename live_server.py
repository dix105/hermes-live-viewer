#!/usr/bin/env python3
"""Static skills viewer + live session chat API."""

from __future__ import annotations

import base64
import fcntl
import hmac
import json
import mimetypes
import os
import pty
import re
import secrets
import select
import sqlite3
import struct
import termios
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

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



import mimetypes
import re

MEDIA_ROOTS = [
    Path("/root/.hermes/cache").resolve(),
    Path("/root/.hermes/images").resolve(),
    Path("/root/.hermes/image_cache").resolve(),
]
_MEDIA_RE = re.compile(
    r"(?:image_url:|saved at:|User sent an image:|User sent a file:|User sent a video:|User sent audio:)\s+(\S+)"
    r"|!\[[^\]]*\]\(([^)]+)\)",
    re.I,
)

def _clean_media_path(raw: str) -> str:
    return (raw or "").strip().strip("[]()<>.,;~`\"'")


def extract_media(text: str) -> list:
    found = []
    seen = set()
    for m in _MEDIA_RE.finditer(text or ""):
        raw = _clean_media_path(m.group(1) or m.group(2) or "")
        if not raw or raw in seen:
            continue
        seen.add(raw)
        name = Path(raw.split("?", 1)[0]).name
        ext = Path(name).suffix.lower()
        if raw.startswith("http://") or raw.startswith("https://"):
            kind = "image" if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"} else ("pdf" if ext == ".pdf" else "link")
            found.append({"kind": kind, "src": raw, "name": name, "exists": True})
            continue
        path = Path(raw).expanduser()
        try:
            resolved = path.resolve()
        except Exception:
            continue
        if not any(str(resolved).startswith(str(root) + os.sep) or resolved == root for root in MEDIA_ROOTS):
            continue
        exists = resolved.is_file()
        if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            kind = "image"
        elif ext == ".pdf":
            kind = "pdf"
        else:
            kind = "file"
        found.append({
            "kind": kind,
            "src": "/api/live/file?p=" + quote(str(resolved), safe=""),
            "name": name,
            "exists": exists,
            "path": str(resolved),
        })
    return found


def display_text(text: str) -> str:
    """Drop the long auto vision dump; keep caption / real words."""
    if not text:
        return ""
    t = text
    t = re.sub(
        r"\[The user sent an image~ Here's what I can see:.*?\]\s*(?:\[If you need a closer look.*?~\])?",
        "",
        t,
        flags=re.S,
    )
    t = re.sub(r"\[The user sent an image[^\]]*\]", "", t)
    t = re.sub(r"\[The user sent a text document:[^\]]*\]", "", t)
    t = re.sub(r"\[If you need a closer look[^\]]*\]", "", t)
    return t.strip()


def safe_media_file(raw: str) -> Path | None:
    try:
        resolved = Path(raw).expanduser().resolve()
    except Exception:
        return None
    if not resolved.is_file():
        return None
    if not any(str(resolved).startswith(str(root) + os.sep) or resolved == root for root in MEDIA_ROOTS):
        return None
    return resolved



PENDING_DIRS = [
    Path("/root/.hermes/cache/images"),
    Path("/root/.hermes/cache/documents"),
]
PENDING_MAX_AGE = 10 * 60


def _filename_claimed(con, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM messages WHERE content LIKE ? LIMIT 1",
        (f"%{name}%",),
    ).fetchone()
    return bool(row)


_PENDING_CACHE = {"t": 0.0, "files": []}

def list_pending_media() -> list:
    now = time.time()
    if now - _PENDING_CACHE["t"] < 0.8:
        return _PENDING_CACHE["files"]
    files = []
    for folder in PENDING_DIRS:
        if not folder.is_dir():
            continue
        for f in folder.iterdir():
            if not f.is_file() or f.name.startswith("."):
                continue
            age = now - f.stat().st_mtime
            if age > PENDING_MAX_AGE:
                continue
            files.append(f)
    if not files:
        return []
    con = db()
    try:
        out = []
        for f in sorted(files, key=lambda x: x.stat().st_mtime):
            if _filename_claimed(con, f.name):
                continue
            ext = f.suffix.lower()
            if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                kind = "image"
            elif ext == ".pdf":
                kind = "pdf"
            else:
                kind = "file"
            out.append({
                "kind": kind,
                "src": "/api/live/file?p=" + quote(str(f.resolve()), safe=""),
                "name": f.name,
                "exists": True,
                "path": str(f.resolve()),
                "mtime": f.stat().st_mtime,
                "pending": True,
            })
        _PENDING_CACHE["t"] = now
        _PENDING_CACHE["files"] = out
        return out
    finally:
        con.close()


def hottest_session_id() -> str | None:
    con = db()
    try:
        row = con.execute(
            """
            SELECT s.id
            FROM sessions s
            JOIN messages m ON m.session_id = s.id
            WHERE COALESCE(s.archived, 0) = 0
              AND (s.source IS NULL OR s.source != 'subagent')
            GROUP BY s.id
            ORDER BY MAX(m.timestamp) DESC
            LIMIT 1
            """
        ).fetchone()
        return row["id"] if row else None
    finally:
        con.close()


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
        pending = list_pending_media()
        hot = hottest_session_id() if pending else None

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
                    "typing": bool(typing or (pending and r["id"] == hot)),
                    "pending_media": pending if r["id"] == hot else [],
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
                items.append({"id": r["id"], "kind": "user", "text": display_text(content), "media": extract_media(content), "timestamp": r["timestamp"]})
                continue

            if role == "assistant":
                tools = parse_tool_calls(r["tool_calls"])
                item = {
                    "id": r["id"],
                    "kind": "assistant",
                    "text": display_text(content),
                    "media": extract_media(content),
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
                        pending[str(t["id"])] = card
                        pending[t["id"]] = card
                items.append(item)
                continue

            if role == "tool":
                cid = r["tool_call_id"]
                output = tool_output(content)
                if cid and str(cid) in pending:
                    cid = str(cid)
                if cid and cid in pending:
                    pending[cid]["output"] = output
                    pending[cid]["status"] = "done"
                    if after and r["id"] > after:
                        pending[cid]["fresh"] = True
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

        updates = []
        if after:
            fresh = []
            for item in items:
                if item["id"] > after:
                    fresh.append(item)
                elif item.get("kind") == "assistant" and item.get("tools"):
                    if any(t.get("fresh") for t in item["tools"]):
                        updates.append(item)
                        fresh.append(item)  # also in items so an old tab still replaces the bubble
            items = fresh

        for card in pending.values():
            if card["status"] == "running":
                running.append({"id": card["id"], "name": card["name"]})

        meta = con.execute(
            "SELECT id, source, parent_session_id, display_name, title, session_key, model FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        info = dict(meta) if meta else {"id": session_id}
        return {"items": items, "updates": updates if after else [], "running_tools": running, "last_id": last_id, "session": info, "usage": session_usage_rows(con, session_id), "pending_media": list_pending_media()}
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



TERM_TOKEN_PATH = ROOT / ".term-token"
_TERM = {}
_TERM_LOCK = threading.Lock()


def get_term_token() -> str:
    if TERM_TOKEN_PATH.exists():
        tok = TERM_TOKEN_PATH.read_text().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(24)
    TERM_TOKEN_PATH.write_text(tok + "\n")
    os.chmod(TERM_TOKEN_PATH, 0o600)
    return tok


def term_ok(qs, headers) -> bool:
    got = (qs.get("token") or [""])[0] or (headers.get("X-Term-Token") or "")
    want = get_term_token()
    if not got or not want:
        return False
    return hmac.compare_digest(str(got), str(want))


class TermSession:
    def __init__(self):
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir("/root")
            os.environ["TERM"] = "xterm-256color"
            os.execv("/bin/bash", ["bash", "-l"])
        self.pid = pid
        self.fd = fd
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.alive = True
        self.touched = time.time()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while self.alive:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.4)
            except Exception:
                break
            if self.fd not in r:
                continue
            try:
                chunk = os.read(self.fd, 8192)
            except OSError:
                break
            if not chunk:
                break
            with self.lock:
                self.buf.extend(chunk)
                if len(self.buf) > 250000:
                    del self.buf[: len(self.buf) - 120000]
        self.alive = False

    def pull(self) -> bytes:
        with self.lock:
            data = bytes(self.buf)
            self.buf.clear()
            return data

    def write(self, data: bytes) -> None:
        self.touched = time.time()
        os.write(self.fd, data)

    def resize(self, rows: int, cols: int) -> None:
        winsize = struct.pack("HHHH", max(2, rows), max(10, cols), 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)

    def close(self) -> None:
        self.alive = False
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.kill(self.pid, 15)
        except OSError:
            pass


def term_get(sid: str) -> TermSession | None:
    with _TERM_LOCK:
        s = _TERM.get(sid)
        if s and time.time() - s.touched > 45 * 60:
            s.close()
            _TERM.pop(sid, None)
            return None
        return s



WS_ROOT = Path("/root").resolve()
WS_SKIP = {
    "node_modules", "venv", ".venv", "env", "__pycache__", ".git",
    "dist", "build", ".next", "target", "vendor", ".cache", "site-packages",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "coverage",
    ".turbo", ".parcel-cache", "bower_components", ".gradle", ".idea",
    ".npm", ".yarn", "Pods", "Carthage", ".terraform", ".uv",
    "hermes-agent/.git",
}
WS_SKIP_FILES = {".env", ".term-token", ".telethon-bot.session"}
WS_MAX_ENTRIES = 400
WS_MAX_FILE = 200_000


def ws_safe(raw: str) -> Path | None:
    if not raw:
        return None
    try:
        resolved = Path(raw).expanduser().resolve()
    except Exception:
        return None
    root = str(WS_ROOT)
    s = str(resolved)
    if s != root and not s.startswith(root + os.sep):
        return None
    return resolved


def ws_list(path: Path) -> dict:
    entries = []
    skipped = 0
    try:
        items = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except PermissionError:
        return {"error": "permission denied", "entries": [], "path": str(path)}
    for f in items:
        name = f.name
        if f.is_dir() and (name in WS_SKIP or (name.startswith(".") and name not in {".hermes", ".openclaw", ".config"})):
            skipped += 1
            continue
        if name in WS_SKIP_FILES:
            continue
        kind = "dir" if f.is_dir() else "file"
        size = 0
        if kind == "file":
            try:
                size = f.stat().st_size
            except OSError:
                continue
        entries.append({"name": name, "path": str(f), "kind": kind, "size": size})
        if len(entries) >= WS_MAX_ENTRIES:
            break
    parent = str(path.parent) if path != WS_ROOT else None
    if parent and not str(Path(parent).resolve()).startswith(str(WS_ROOT)):
        parent = None
    return {"path": str(path), "parent": parent, "entries": entries, "skipped": skipped}


def ws_read(path: Path) -> dict:
    if not path.is_file():
        return {"error": "not a file"}
    if path.name in WS_SKIP_FILES or path.suffix in {".sqlite", ".db", ".pyc", ".so", ".woff", ".woff2"}:
        return {"error": "skipped binary/secret"}
    try:
        size = path.stat().st_size
    except OSError as e:
        return {"error": str(e)}
    if size > WS_MAX_FILE:
        return {"error": f"file too large ({size} bytes, cap {WS_MAX_FILE})"}
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        return {"error": "binary file"}
    return {"path": str(path), "content": data.decode("utf-8", errors="replace"), "size": size}



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

        if path == "/api/live/file":
            raw = (qs.get("p") or [""])[0]
            fpath = safe_media_file(raw)
            if not fpath:
                self._json({"error": "not found"}, 404)
                return
            ctype = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
            data = fpath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("Content-Disposition", f'inline; filename="{fpath.name}"')
            self.end_headers()
            self.wfile.write(data)
            return

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

        if path == "/api/term/out":
            if not term_ok(qs, self.headers):
                self._json({"error": "unauthorized"}, 401)
                return
            sid = (qs.get("sid") or [""])[0]
            sess = term_get(sid)
            if not sess:
                self._json({"error": "no session"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                idle = 0
                while idle < 200:
                    data = sess.pull()
                    if data:
                        idle = 0
                        b64 = base64.b64encode(data).decode("ascii")
                        self.wfile.write(("data: " + b64 + chr(10) + chr(10)).encode("ascii"))
                        self.wfile.flush()
                    else:
                        idle += 1
                        self.wfile.write(b": ping" + bytes((10, 10)))
                        self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                return
            return

        if path == "/api/live/workspace":
            raw = (qs.get("path") or ["/root"])[0]
            folder = ws_safe(raw)
            if not folder or not folder.is_dir():
                self._json({"error": "not found"}, 404)
                return
            self._json(ws_list(folder))
            return

        if path == "/api/live/workspace/file":
            raw = (qs.get("path") or [""])[0]
            fpath = ws_safe(raw)
            if not fpath:
                self._json({"error": "not found"}, 404)
                return
            self._json(ws_read(fpath))
            return

        if path == "/workspace":
            self.path = "/workspace.html"
        if path == "/term":
            self.path = "/term.html"
        if path == "/live":
            self.path = "/live.html"
        if path == "/cron":
            self.path = "/cron.html"
        if path == "/status":
            self.path = "/status.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""

        if path == "/api/term/open":
            if not term_ok(qs, self.headers):
                self._json({"error": "unauthorized"}, 401)
                return
            sid = secrets.token_hex(8)
            with _TERM_LOCK:
                if len(_TERM) >= 3:
                    old = next(iter(_TERM))
                    _TERM.pop(old).close()
                _TERM[sid] = TermSession()
            self._json({"id": sid})
            return

        if path == "/api/term/in":
            if not term_ok(qs, self.headers):
                self._json({"error": "unauthorized"}, 401)
                return
            sess = term_get((qs.get("sid") or [""])[0])
            if not sess:
                self._json({"error": "no session"}, 404)
                return
            if raw:
                sess.write(raw)
            self._json({"ok": True})
            return

        if path == "/api/term/resize":
            if not term_ok(qs, self.headers):
                self._json({"error": "unauthorized"}, 401)
                return
            sess = term_get((qs.get("sid") or [""])[0])
            if not sess:
                self._json({"error": "no session"}, 404)
                return
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = {}
            sess.resize(int(body.get("rows") or 24), int(body.get("cols") or 80))
            self._json({"ok": True})
            return

        self._json({"error": "not found"}, 404)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", 8471), Handler)
    get_term_token()
    print("skills+live http://0.0.0.0:8471/  chat=/live term=/term", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
