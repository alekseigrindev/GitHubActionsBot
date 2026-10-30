import hashlib
import hmac
import html
import json
import os
import re
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler


def post_json(url, payload, headers=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    urllib.request.urlopen(req, timeout=4).read()


def telegram_chat_ids():
    # TELEGRAM_CHAT_ID may hold several ids separated by ";", e.g. "111;222;-100333".
    raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    return [part.strip() for part in raw.split(";") if part.strip()]


def send_telegram(text):
    token = os.environ["TELEGRAM_TOKEN"]
    chat_ids = telegram_chat_ids()
    if not chat_ids:
        raise RuntimeError("TELEGRAM_CHAT_ID is empty")
    for chat_id in chat_ids:
        # One failing recipient (e.g. someone who never pressed Start) must not block the rest.
        try:
            post_json(
                f"https://api.telegram.org/bot{token}/sendMessage",
                {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        except Exception as exc:
            print(f"telegram send to {chat_id} failed: {exc!r}")


def send_discord(text):
    # Optional: Discord is skipped when the webhook URL is not configured.
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    post_json(
        url,
        {
            "content": text,
            # Never let a branch name or user trigger @everyone / @here pings.
            "allowed_mentions": {"parse": []},
        },
        # Discord often rejects the default Python User-Agent with a 403.
        headers={"User-Agent": "deploy-notify"},
    )


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_duration(seconds):
    seconds = max(int(seconds), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min {secs} s"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def run_duration(run):
    # Duration is computed from the run's start time and its last update time.
    try:
        started = parse_time(run["run_started_at"])
        finished = parse_time(run["updated_at"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return format_duration((finished - started).total_seconds())


def build_parts(data, action):
    """Collect the message content once; each messenger formats it its own way."""
    run = data["workflow_run"]
    actor = (run.get("actor") or {}).get("login") or "?"
    branch = run.get("head_branch") or "?"
    conclusion = run.get("conclusion") or "unknown"

    if action == "requested":
        icon, status = "⏳", "Queued"
    elif action == "in_progress":
        icon, status = "🚀", "In progress"
    else:
        icon = {
            "success": "✅",
            "failure": "❌",
            "cancelled": "🚫",
            "timed_out": "⏰",
        }.get(conclusion, "⚠️")
        status = conclusion.replace("_", " ").capitalize()

    # (icon, label, value): every line except the link gets its own icon.
    lines = [
        ("👤", "User", actor),
        ("🌿", "Branch", branch),
        ("📊", "Status", status),
    ]
    if action == "completed":
        duration = run_duration(run)
        if duration:
            lines.append(("⏱️", "Duration", duration))

    return {
        "icon": icon,
        "repo": data["repository"]["full_name"],
        "workflow": run.get("name") or "?",
        "lines": lines,
        "url": run["html_url"],
    }


def telegram_text(parts):
    esc = html.escape
    body = "\n".join(f"{icon} {label}: {esc(value)}" for icon, label, value in parts["lines"])
    title = f"{parts['icon']} <b>{esc(parts['repo'])}</b>"
    workflow = f"⚙️ Workflow: <b><i>{esc(parts['workflow'])}</i></b>"
    return f"{title}\n\n{workflow}\n\n{body}\n\n{esc(parts['url'])}"


def escape_discord(text):
    # Keep names like my_repo_name from turning into italics, etc.
    return re.sub(r"([\\*_~`>|])", r"\\\1", text)


def discord_text(parts):
    body = "\n".join(
        f"{icon} {label}: {escape_discord(value)}" for icon, label, value in parts["lines"]
    )
    # <url> stops Discord from expanding the link into a big preview card.
    title = f"{parts['icon']} **{escape_discord(parts['repo'])}**"
    workflow = f"⚙️ Workflow: ***{escape_discord(parts['workflow'])}***"
    return f"{title}\n\n{workflow}\n\n{body}\n\n<{parts['url']}>"


class handler(BaseHTTPRequestHandler):
    def _reply(self, code, text="ok"):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Quick browser check that the function is deployed.
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
        # requested: queued; in_progress: started running; completed: finished.
        if action not in ("requested", "in_progress", "completed"):
            return self._reply(200, "ignored")

        parts = build_parts(data, action)
        channels = (
            ("telegram", send_telegram, telegram_text(parts)),
            ("discord", send_discord, discord_text(parts)),
        )
        for name, send, text in channels:
            # Each channel has its own try/except: one failing must not block the other.
            try:
                send(text)
            except Exception as exc:
                # The error shows up in the Vercel logs; GitHub still gets a fast response.
                print(f"{name} send failed: {exc!r}")

        self._reply(200)
