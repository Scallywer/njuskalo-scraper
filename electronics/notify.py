"""
Telegram notifier for the watchlist.

Reads credentials from the environment (kept out of git in `.env`):
    TELEGRAM_BOT_TOKEN   from @BotFather
    TELEGRAM_CHAT_ID     your chat id (use `python -m electronics.notify --get-chat-id`)

If the credentials aren't set, sending is a no-op (so the pipeline still runs).

CLI helpers:
    python -m electronics.notify --get-chat-id   # after you message your bot once
    python -m electronics.notify --test          # send a test message
"""

import os
import sys
import requests

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 3800  # Telegram hard-limits messages at 4096 chars


def _creds():
    return os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def _chunks(text, n=MAX_LEN):
    lines, buf = text.split("\n"), ""
    for ln in lines:
        if len(buf) + len(ln) + 1 > n:
            if buf:
                yield buf
            buf = ln
        else:
            buf = f"{buf}\n{ln}" if buf else ln
    if buf:
        yield buf


def send(text: str) -> bool:
    """Send a plain-text message to the configured chat. No-op if unconfigured."""
    token, chat = _creds()
    if not token or not chat:
        print("[notify] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping push")
        return False
    ok = True
    for chunk in _chunks(text):
        try:
            r = requests.post(
                API.format(token=token, method="sendMessage"),
                json={"chat_id": chat, "text": chunk, "disable_web_page_preview": True},
                timeout=20)
            if r.status_code != 200:
                print(f"[notify] telegram error {r.status_code}: {r.text[:200]}")
                ok = False
        except Exception as e:
            print(f"[notify] telegram send failed: {type(e).__name__}: {e}")
            ok = False
    return ok


def get_chat_id():
    """Print chat ids from recent updates (message your bot first, then run this)."""
    token, _ = _creds()
    if not token:
        print("Set TELEGRAM_BOT_TOKEN first.")
        return
    r = requests.get(API.format(token=token, method="getUpdates"), timeout=20)
    data = r.json()
    seen = set()
    for upd in data.get("result", []):
        chat = (upd.get("message") or upd.get("channel_post") or {}).get("chat", {})
        if chat.get("id") and chat["id"] not in seen:
            seen.add(chat["id"])
            print(f"chat_id={chat['id']}  ({chat.get('type')}, "
                  f"{chat.get('username') or chat.get('title') or chat.get('first_name')})")
    if not seen:
        print("No chats found. Send a message to your bot in Telegram, then re-run.")


if __name__ == "__main__":
    if "--get-chat-id" in sys.argv:
        get_chat_id()
    elif "--test" in sys.argv:
        print("sent" if send("✅ njuskalo watch bot is wired up.") else "not sent")
    else:
        print(__doc__)
