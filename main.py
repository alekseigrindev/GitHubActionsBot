import hashlib
import hmac
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler


def send_telegram(text):
    token = os.environ["TELEGRAM_TOKEN"]
    payload = json.dumps({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        "text": text,
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=5).read()


def build_message(data):
    run = data["workflow_run"]
    conclusion = run.get("conclusion") or "unknown"
    icon = {"success": "✅", "failure": "❌"}.get(conclusion, "⚠️")
    actor = (run.get("actor") or {}).get("login", "?")
    return (
        f"{icon} {data['repository']['full_name']}\n"
        f"{run['name']} · ветка {run['head_branch']} · {actor}\n"
        f"Результат: {conclusion}\n"
        f"{run['html_url']}"
    )


class handler(BaseHTTPRequestHandler):
    def _reply(self, code, text="ok"):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Для быстрой проверки в браузере, что функция задеплоена.
        self._reply(200, "deploy-notify is up")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()
        expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        received = self.headers.get("X-Hub-Signature-256", "")
        if not secret or not hmac.compare_digest(expected, received):
            return self._reply(401, "bad signature")

        if self.headers.get("X-GitHub-Event") != "workflow_run":
            return self._reply(200, "ignored")

        data = json.loads(body)
        if data.get("action") != "completed":
            return self._reply(200, "ignored")

        try:
            send_telegram(build_message(data))
        except Exception as exc:
            # Ошибка попадёт в логи Vercel; GitHub всё равно получает быстрый ответ.
            print(f"telegram send failed: {exc!r}")

        self._reply(200)