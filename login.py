#!/usr/bin/env python3
"""One-time Telegram login. Run this ONCE, interactively, on the secondary number.

It mints the Telethon .session file the daemon reuses forever. You'll be asked for
the phone number, the code Telegram sends, and your 2FA password (if set).

    python3 login.py
"""
import json
import os
import sys

from telethon import TelegramClient

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    path = os.path.join(HERE, "config.json")
    if not os.path.exists(path):
        sys.exit("config.json not found. Copy config.example.json -> config.json and fill it in first.")
    with open(path) as f:
        return json.load(f)


def main():
    cfg = load_config()
    if not cfg.get("api_id") or "HASH" in str(cfg.get("api_hash", "")):
        sys.exit("Fill in real api_id / api_hash in config.json (get them from https://my.telegram.org).")

    session_path = os.path.join(HERE, cfg.get("session_name", "tgclaude"))
    client = TelegramClient(session_path, int(cfg["api_id"]), cfg["api_hash"])
    client.start()  # interactive: prompts for phone, code, and 2FA password
    me = client.loop.run_until_complete(client.get_me())
    print("\nLogged in as: %s  (id=%s, @%s)" % (me.first_name, me.id, me.username))
    print("Session saved to: %s.session" % session_path)
    print("You can now run the daemon. Keep the .session file secret (it is full account access).")
    client.disconnect()


if __name__ == "__main__":
    main()
