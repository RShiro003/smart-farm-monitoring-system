import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse


WEBHOOK_ENV_NAME = "DISCORD_WEBHOOK_URL"
DISCORD_WEBHOOK_HOSTS = {
    "discord.com",
    "discordapp.com",
    "canary.discord.com",
    "ptb.discord.com",
}

# Match the stable first line stored by build_summary_message in the outbox.
# Formatting at delivery time also supports summaries queued before this change.
SUMMARY_STYLES = {
    "[스마트팜 일간 요약]": ("🟦 스마트팜 일간 요약 · 1일", 0x3498DB),
    "[스마트팜 주간 요약]": ("🟪 스마트팜 주간 요약 · 7일", 0x9B59B6),
}


def _text_chunks(text, limit):
    """Split without dropping characters; count emoji conservatively as UTF-16."""
    start = 0
    units = 0
    for index, character in enumerate(text):
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > limit:
            yield text[start:index]
            start = index
            units = 0
        units += width
    yield text[start:]


def split_discord_message(message):
    """Return independently deliverable parts, retaining summary style per part."""
    heading, separator, body = message.partition("\n")
    if heading in SUMMARY_STYLES and separator and body:
        return [heading + "\n" + part for part in _text_chunks(body, 4096)]
    return list(_text_chunks(message, 2000))


def _message_payload(message):
    """Style only known summaries; preserve ordinary alerts and stored text."""
    heading, separator, body = message.partition("\n")
    style = SUMMARY_STYLES.get(heading)
    if style is None or not separator or not body:
        return {"content": message}
    title, color = style
    return {"embeds": [{"title": title, "description": body, "color": color}]}


def discord_webhook_url_is_allowed(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value.strip())
        return (
            parsed.scheme == "https"
            and parsed.hostname in DISCORD_WEBHOOK_HOSTS
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path.startswith("/api/webhooks/")
        )
    except ValueError:
        return False


def resolve_webhook_url(webhook_url=None):
    # 대시보드에서 저장한 URL이 있으면 그것을 쓰고, 없으면 환경변수로 돌아간다.
    # 설정 화면을 쓰지 않는 기존 배포는 예전과 똑같이 동작한다.
    if isinstance(webhook_url, str) and webhook_url.strip():
        return webhook_url.strip()
    return os.environ.get(WEBHOOK_ENV_NAME, "").strip()


def send_discord_message(message, webhook_url=None):
    """Discord Webhook으로 알림 메시지를 보낸다.

    Webhook URL이 없거나 Discord 호출이 실패해도 센서 저장 요청은 실패하면 안 된다.
    그래서 이 함수는 예외를 밖으로 던지지 않고 콘솔 로그와 False 반환으로만 알린다.

    webhook_url을 넘기면 장치별로 다른 채널에 보낼 수 있다.
    """
    webhook_url = resolve_webhook_url(webhook_url)
    if not webhook_url:
        print(f"[Discord] {WEBHOOK_ENV_NAME} is not set. Alert skipped.")
        return False
    if not discord_webhook_url_is_allowed(webhook_url):
        print("[Discord] Refused non-Discord webhook URL.")
        return False

    # Outbox jobs are split before sending, so completed parts are not resent
    # when another part fails. Direct callers also get size-safe delivery.
    for part in split_discord_message(message):
        if not _send_payload(_message_payload(part), webhook_url):
            return False
    return True


def _send_payload(payload, webhook_url):
    payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "smart-farm-monitoring-system",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if 200 <= response.status < 300:
                return True
            print(f"[Discord] Webhook returned HTTP {response.status}.")
            return False
    except urllib.error.HTTPError as e:
        print(f"[Discord] Webhook failed with HTTP {e.code}.")
        e.close()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[Discord] Webhook request failed: {e}")
    return False
