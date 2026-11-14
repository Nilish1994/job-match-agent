"""Minimal Telegram Bot API client (long polling, inline keyboards, documents)."""
import html

import httpx


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, client: httpx.Client):
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = client

    def _call(self, method: str, http_timeout: float = 30, files=None, **params):
        payload = {key: value for key, value in params.items() if value is not None}
        if files:
            data = {key: str(value) for key, value in payload.items()}
            response = self.client.post(f"{self.base}/{method}", data=data, files=files, timeout=http_timeout)
        else:
            response = self.client.post(f"{self.base}/{method}", json=payload, timeout=http_timeout)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not body.get("ok"):
            # Never include the URL: it contains the bot token.
            raise TelegramError(f"{method} failed: HTTP {response.status_code} {body.get('description', '')}".strip())
        return body["result"]

    def updates(self, offset: int | None, wait: int = 25) -> list[dict]:
        # `timeout` here is Telegram's long-poll wait; the HTTP timeout must outlast it.
        return self._call("getUpdates", http_timeout=wait + 10, offset=offset, timeout=wait,
                          allowed_updates=["message", "callback_query"])

    def send(self, chat_id: str, text: str, buttons: list[list[dict]] | None = None) -> dict:
        markup = {"inline_keyboard": buttons} if buttons else None
        return self._call("sendMessage", chat_id=chat_id, text=text[:4096], parse_mode="HTML",
                          disable_web_page_preview=True, reply_markup=markup)

    def edit_buttons(self, chat_id: str, message_id: int, buttons: list[list[dict]] | None) -> None:
        self._call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                   reply_markup={"inline_keyboard": buttons or []})

    def answer(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])

    def get_me(self) -> dict:
        return self._call("getMe")

    def download(self, file_id: str, max_bytes: int) -> bytes:
        """Fetch a file the user sent. Size is checked before and after downloading."""
        info = self._call("getFile", file_id=file_id)
        if (info.get("file_size") or 0) > max_bytes:
            raise TelegramError("file too large")
        response = self.client.get(f"{self.base.replace('/bot', '/file/bot', 1)}/{info['file_path']}", timeout=60)
        if response.status_code != 200 or len(response.content) > max_bytes:
            raise TelegramError(f"download failed: HTTP {response.status_code}")
        return response.content

    def edit_text(self, chat_id: str, message_id: int, text: str, buttons: list[list[dict]] | None = None) -> None:
        self._call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4096], parse_mode="HTML",
                   disable_web_page_preview=True, reply_markup={"inline_keyboard": buttons} if buttons else None)

    def send_document(self, chat_id: str, filename: str, content: bytes, caption: str = "",
                      mime: str = "application/pdf") -> None:
        self._call("sendDocument", http_timeout=60, files={"document": (filename, content, mime)},
                   chat_id=chat_id, caption=caption[:1024])



def button(text: str, data: str | None = None, url: str | None = None) -> dict:
    if url:
        return {"text": text, "url": url}
    if data is None or len(data.encode()) > 64:
        raise ValueError("callback data must be 1-64 bytes")
    return {"text": text, "callback_data": data}
