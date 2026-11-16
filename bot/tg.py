"""Minimal Telegram Bot API client (standard library only)."""
import json
import os
import time
import urllib.error
import urllib.request

API = "https://api.telegram.org"


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str):
        self.token = token
        self.base = f"{API}/bot{token}"

    def call(self, method: str, http_timeout: int = 30, **params):
        params = {k: v for k, v in params.items() if v is not None}
        data = json.dumps(params).encode()
        req = urllib.request.Request(
            f"{self.base}/{method}", data=data,
            headers={"Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=http_timeout) as r:
                    res = json.loads(r.read().decode())
                return res.get("result")
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="ignore")
                try:
                    info = json.loads(body)
                except Exception:
                    info = {}
                retry = (info.get("parameters") or {}).get("retry_after")
                if e.code == 429 and retry:
                    time.sleep(int(retry) + 1)
                    continue
                if e.code >= 500 and attempt < 2:
                    time.sleep(2)
                    continue
                raise TelegramError(f"{method}: {e.code} {body[:300]}")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 2:
                    time.sleep(2)
                    continue
                raise TelegramError(f"{method}: {e}")

    # ---- helpers -------------------------------------------------------
    def send(self, chat_id, text, buttons=None, reply_to=None, force_reply=False):
        markup = None
        if buttons:
            markup = {"inline_keyboard": buttons}
        elif force_reply:
            markup = {"force_reply": True, "selective": True}
        return self.call("sendMessage", chat_id=chat_id, text=text,
                         parse_mode="HTML", disable_web_page_preview=True,
                         reply_markup=markup,
                         reply_parameters={"message_id": reply_to,
                                           "allow_sending_without_reply": True}
                         if reply_to else None)

    def edit(self, chat_id, message_id, text, buttons=None):
        try:
            return self.call("editMessageText", chat_id=chat_id,
                             message_id=message_id, text=text,
                             parse_mode="HTML", disable_web_page_preview=True,
                             reply_markup={"inline_keyboard": buttons or []})
        except TelegramError as e:
            if "message is not modified" in str(e):
                return None
            raise

    def answer(self, callback_id, text=None, alert=False):
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id,
                      text=text, show_alert=alert)
        except TelegramError:
            pass

    def copy(self, to_chat, from_chat, message_id, reply_to=None):
        return self.call("copyMessage", chat_id=to_chat, from_chat_id=from_chat,
                         message_id=message_id,
                         reply_parameters={"message_id": reply_to,
                                           "allow_sending_without_reply": True}
                         if reply_to else None)

    def download(self, file_id) -> bytes:
        info = self.call("getFile", file_id=file_id)
        url = f"{API}/file/bot{self.token}/{info['file_path']}"
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.read()

    def updates(self, offset, timeout=20):
        return self.call("getUpdates", http_timeout=timeout + 15,
                         offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"]) or []


def from_env() -> Telegram:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN secret is missing")
    return Telegram(token)
