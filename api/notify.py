import hashlib
import hmac
import html
import json
import os
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler


def send_telegram(text):
    token = os.environ["TELEGRAM_TOKEN"]
    payload = json.dumps({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=5).read()


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_duration(seconds):
    seconds = max(int(seconds), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} ч {minutes} мин {secs} с"
    if minutes:
        return f"{minutes} мин {secs} с"
    return f"{secs} с"


def run_duration(run):
    # Время считаем по полям запуска: начало и последнее обновление.
    try:
        started = parse_time(run["run_started_at"])
        finished = parse_time(run["updated_at"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return format_duration((finished - started).total_seconds())


def build_message(data, action):
    run = data["workflow_run"]
    esc = html.escape
    actor = (run.get("actor") or {}).get("login") or "?"
    branch = run.get("head_branch") or "?"
    conclusion = run.get("conclusion") or "unknown"

    if action == "in_progress":
        icon, status = "🚀", "Запущен"
    else:
        icon = {"success": "✅", "failure": "❌"}.get(conclusion, "⚠️")
        status = f"Результат: {conclusion}"

    lines = [
        f"{icon} <b>{esc(data['repository']['full_name'])}</b>",
        f"Пользователь: {esc(actor)}",
        f"Ветка: {esc(branch)}",
        f"Workflow: {esc(run.get('name') or '?')}",
        esc(status),
    ]
    if action == "completed":
        duration = run_duration(run)
        if duration:
            lines.append(f"Время выполнения: {duration}")
    lines.append(esc(run["html_url"]))
    return "\n".join(lines)


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
        action = data.get("action")
        # in_progress: пайплайн начал выполняться; completed: закончился.
        if action not in ("in_progress", "completed"):
            return self._reply(200, "ignored")

        try:
            send_telegram(build_message(data, action))
        except Exception as exc:
            # Ошибка попадёт в логи Vercel; GitHub всё равно получает быстрый ответ.
            print(f"telegram send failed: {exc!r}")

        self._reply(200)
