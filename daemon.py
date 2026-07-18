#!/usr/bin/env python3
"""Master orchestrator daemon.

One always-on asyncio process that:
  * owns the Telegram userbot (single inbound handler + single outbound sender),
  * routes each incoming message by chat_id to that client's resumable Claude
    session (the "wake"): chat_id -> SQLite row -> (cwd, session_id) -> claude -p,
  * serialises messages per client (one turn at a time) but runs up to
    max_concurrent clients in parallel,
  * delivers Claude's replies by watching each client's outbox/ folder.

Run:  python3 daemon.py     (after `python3 login.py` has created the .session)
"""
import asyncio
import json
import os
import random
import re
import sys
import time
import logging
from logging.handlers import RotatingFileHandler

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

import db as dbmod
from templates import scaffold_client
from worker import run_turn

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
GUARD_HOOK = os.path.join(HERE, "guard_hook.py")
TG_TEXT_LIMIT = 4000  # stay under Telegram's 4096 hard limit


def setup_logging():
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    handler = RotatingFileHandler(
        os.path.join(HERE, "logs", "daemon.log"), maxBytes=5_000_000, backupCount=5
    )
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(fmt)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    log = logging.getLogger("tgclaude")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.addHandler(stream)
    return log


log = setup_logging()


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        sys.exit("config.json not found. Copy config.example.json -> config.json and fill it in.")
    with open(CONFIG_PATH) as f:
        return json.load(f)


def allowlist_now() -> list:
    """Re-read the allowlist from disk on every message so the operator can add or
    remove clients live, with no restart."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f).get("allowlist", []) or []
    except Exception:
        return []


def is_allowed(sender_id, username) -> bool:
    uname = (username or "").lstrip("@").lower()
    for entry in allowlist_now():
        eid = entry.get("id")
        euser = (entry.get("username") or "").lstrip("@").lower()
        if eid is not None and sender_id is not None and int(eid) == int(sender_id):
            return True
        if euser and uname and euser == uname:
            return True
    return False


def safe_username(username) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", username or "anon") or "anon"


class Daemon:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.db = dbmod.Db(os.path.join(HERE, "state.db"))
        self.clients_root = dbmod.expand(cfg.get("clients_root", "~/clients"))
        os.makedirs(self.clients_root, exist_ok=True)
        self.queues = {}    # chat_id -> asyncio.Queue
        self.consumers = {}  # chat_id -> asyncio.Task
        self.sem = asyncio.Semaphore(int(cfg.get("max_concurrent", 2)))
        self.spend_cap = float(cfg.get("spend_cap_usd", 20.0))
        session_path = os.path.join(HERE, cfg.get("session_name", "tgclaude"))
        self.client = TelegramClient(session_path, int(cfg["api_id"]), cfg["api_hash"])

    # ---------- Telegram egress (single point, paced + flood-safe) ----------
    async def alert_operator(self, text: str):
        try:
            await self.client.send_message(self.cfg.get("operator_chat_id", "me"), text)
        except Exception as e:
            log.error("operator alert failed: %s", e)

    async def _send_text(self, chat_id: int, text: str):
        text = text or ""
        for i in range(0, max(len(text), 1), TG_TEXT_LIMIT):
            chunk = text[i:i + TG_TEXT_LIMIT]
            await self._flood_safe(self.client.send_message(chat_id, chunk))
            await asyncio.sleep(random.uniform(0.4, 1.1))

    async def _send_file(self, chat_id: int, path: str, caption: str):
        await self._flood_safe(self.client.send_file(chat_id, path, caption=(caption or "")[:1024]))

    async def _flood_safe(self, coro):
        try:
            return await coro
        except FloodWaitError as e:
            log.warning("FloodWait %ss — backing off", e.seconds)
            await asyncio.sleep(e.seconds + 1)
            return await coro

    # ---------- routing ----------
    def resolve_or_create(self, chat_id: int, username):
        row = self.db.get_client(chat_id)
        if row:
            return row, False
        cwd = os.path.join(self.clients_root, "%d__%s" % (chat_id, safe_username(username)))
        scaffold_client(cwd, GUARD_HOOK)
        import uuid
        session_id = str(uuid.uuid4())
        self.db.create_client(chat_id, username, session_id, cwd)
        log.info("onboarded new client %s (@%s) -> %s", chat_id, username, cwd)
        return self.db.get_client(chat_id), True

    def ensure_consumer(self, chat_id: int):
        if chat_id not in self.queues:
            self.queues[chat_id] = asyncio.Queue()
        task = self.consumers.get(chat_id)
        if task is None or task.done():
            self.consumers[chat_id] = asyncio.create_task(self._consume(chat_id))

    async def _consume(self, chat_id: int):
        q = self.queues[chat_id]
        while True:
            msg_id = await q.get()
            try:
                await self._process_turn(chat_id, msg_id)
            except Exception as e:
                log.exception("process_turn crashed for %s: %s", chat_id, e)
                await self.alert_operator("⚠️ turn crashed for chat %s: %s" % (chat_id, e))
            finally:
                q.task_done()

    async def _process_turn(self, chat_id: int, msg_id: int):
        client_row = self.db.get_client(chat_id)
        inbox_row = self.db.get_inbox(msg_id)
        if client_row is None or inbox_row is None:
            return
        if client_row["status"] in ("blocked", "capped"):
            self.db.set_inbox_status(msg_id, "done")
            log.info("skipping %s — status=%s", chat_id, client_row["status"])
            return

        turn_id = self.db.open_turn(chat_id, msg_id, client_row["session_id"])
        log.info("turn start chat=%s msg=%s resume=%s", chat_id, msg_id, client_row["started"] == 1)
        async with self.sem:
            res = await run_turn(self.cfg, client_row, inbox_row)

        if res["ok"]:
            if client_row["started"] == 0:
                self.db.mark_started(chat_id)
            total = self.db.bump_spend(chat_id, res["cost"])
            self.db.add_metric(chat_id, "cost", res["cost"])
            self.db.close_turn(turn_id, "delivered", res["cost"])
            self.db.set_inbox_status(msg_id, "done")
            log.info("turn ok chat=%s cost=$%.4f total=$%.4f", chat_id, res["cost"], total)
            if total >= self.spend_cap:
                self.db.set_status(chat_id, "capped")
                await self.alert_operator(
                    "🛑 client %s hit spend cap ($%.2f). Status=capped, paused." % (chat_id, total)
                )
        else:
            self.db.close_turn(turn_id, "needs_review", res["cost"])
            self.db.set_inbox_status(msg_id, "error")
            log.error("turn FAILED chat=%s: %s", chat_id, res["error"])
            await self.alert_operator("⚠️ turn failed for chat %s:\n%s" % (chat_id, res["error"]))

    # ---------- outbox delivery (Claude -> client) ----------
    async def outbox_watcher(self):
        while True:
            try:
                await self._scan_outboxes()
            except Exception as e:
                log.exception("outbox watcher error: %s", e)
            await asyncio.sleep(1.5)

    async def _scan_outboxes(self):
        for chat_id, cwd in self.db.all_client_dirs():
            outdir = os.path.join(cwd, "outbox")
            if not os.path.isdir(outdir):
                continue
            for name in sorted(os.listdir(outdir)):
                if not name.endswith(".json"):
                    continue
                fpath = os.path.join(outdir, name)
                if not os.path.isfile(fpath):
                    continue
                await self._deliver_outbox_file(chat_id, cwd, fpath, name)

    async def _deliver_outbox_file(self, chat_id, cwd, fpath, name):
        try:
            with open(fpath) as f:
                payload = json.load(f)
        except Exception:
            # Likely a half-written file — retry next pass. If it stays broken, quarantine.
            try:
                age = time.time() - os.path.getmtime(fpath)
            except OSError:
                return
            if os.path.getsize(fpath) > 0 and age > 20:
                self._quarantine(cwd, fpath, name)
                await self.alert_operator("⚠️ unparseable outbox file from chat %s: %s" % (chat_id, name))
            return

        kind = payload.get("type")
        try:
            if kind == "message":
                await self._send_text(chat_id, str(payload.get("text", "")))
            elif kind == "file":
                rel = str(payload.get("path", ""))
                target = rel if os.path.isabs(rel) else os.path.join(cwd, rel)
                target_real = os.path.realpath(target)
                cwd_real = os.path.realpath(cwd)
                # SECURITY: the daemon has full FS access — never send a file that
                # resolves outside the client's own folder (prevents exfiltrating
                # the .session file or another client's code via a crafted path).
                if target_real != cwd_real and not target_real.startswith(cwd_real + os.sep):
                    self._quarantine(cwd, fpath, name)
                    await self.alert_operator(
                        "🛑 blocked outbox file from chat %s pointing OUTSIDE workspace: %s" % (chat_id, rel)
                    )
                    return
                if not os.path.isfile(target_real):
                    self._quarantine(cwd, fpath, name)
                    await self.alert_operator("⚠️ outbox file not found for chat %s: %s" % (chat_id, rel))
                    return
                await self._send_file(chat_id, target_real, str(payload.get("caption", "")))
            else:
                self._quarantine(cwd, fpath, name)
                return
            # delivered — move out of the way so it is not resent
            self._mark_sent(cwd, fpath, name)
            log.info("delivered outbox %s to chat %s (%s)", name, chat_id, kind)
        except Exception as e:
            log.exception("failed delivering %s to %s: %s", name, chat_id, e)
            await self.alert_operator("⚠️ failed delivering %s to chat %s: %s" % (name, chat_id, e))

    def _mark_sent(self, cwd, fpath, name):
        dst = os.path.join(cwd, "outbox", ".sent", name)
        try:
            os.replace(fpath, dst)
        except OSError:
            try:
                os.remove(fpath)
            except OSError:
                pass

    def _quarantine(self, cwd, fpath, name):
        baddir = os.path.join(cwd, "outbox", ".bad")
        os.makedirs(baddir, exist_ok=True)
        try:
            os.replace(fpath, os.path.join(baddir, name))
        except OSError:
            pass

    # ---------- inbound handler ----------
    def register_handlers(self):
        @self.client.on(events.NewMessage(incoming=True))
        async def handler(event):
            if event.out or not event.is_private:
                return
            sender = await event.get_sender()
            sender_id = event.sender_id
            username = getattr(sender, "username", None)
            if not is_allowed(sender_id, username):
                log.info("dropped message from non-allowlisted %s (@%s)", sender_id, username)
                return

            client_row, _is_new = self.resolve_or_create(sender_id, username)

            media_paths = []
            if event.media:
                inbox_dir = os.path.join(client_row["cwd"], "inbox")
                os.makedirs(inbox_dir, exist_ok=True)
                try:
                    saved = await event.download_media(file=inbox_dir + os.sep)
                    if saved:
                        media_paths.append(saved)
                except Exception as e:
                    log.warning("media download failed for %s: %s", sender_id, e)

            text = event.raw_text or ""
            msg_id = self.db.insert_inbox(sender_id, text, json.dumps(media_paths))
            self.ensure_consumer(sender_id)
            self.queues[sender_id].put_nowait(msg_id)
            log.info("queued msg=%s chat=%s len=%s media=%s", msg_id, sender_id, len(text), len(media_paths))

    async def recover_queued(self):
        """On startup, re-enqueue any inbox rows that were queued but never finished."""
        rows = self.db.queued_inbox()
        for r in rows:
            self.ensure_consumer(r["chat_id"])
            self.queues[r["chat_id"]].put_nowait(r["msg_id"])
        if rows:
            log.info("recovered %s queued message(s) from previous run", len(rows))

    async def run(self):
        if os.environ.get("ANTHROPIC_API_KEY"):
            log.warning("ANTHROPIC_API_KEY is set in the environment! It is stripped per-turn so "
                        "billing stays on your subscription, but you should unset it to be safe.")
        await self.client.connect()
        if not await self.client.is_user_authorized():
            sys.exit("Not logged in. Run `python3 login.py` once on the secondary number first.")
        me = await self.client.get_me()
        log.info("daemon online as @%s (id=%s)", me.username, me.id)
        self.db.reset_in_flight()
        self.register_handlers()
        asyncio.create_task(self.outbox_watcher())
        await self.recover_queued()
        await self.alert_operator("✅ tgclaude daemon online. Serving allowlisted clients.")
        await self.client.run_until_disconnected()


def main():
    cfg = load_config()
    daemon = Daemon(cfg)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        log.info("shutting down (keyboard interrupt)")


if __name__ == "__main__":
    main()
