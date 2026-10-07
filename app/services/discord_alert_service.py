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


def _post_webhook(webhook_url, body):
    """Webhook에 JSON body를 보내고 (성공 여부, HTTP 상태 코드)를 돌려준다."""
    # 요약 본문에 장치 별칭 등 사용자 입력이 섞이므로 @everyone 같은 멘션은 항상 막는다.
    body = {**body, "allowed_mentions": {"parse": []}}
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "smart-farm-monitoring-system",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if 200 <= response.status < 300:
                return True, response.status
            print(f"[Discord] Webhook returned HTTP {response.status}.")
            return False, response.status
    except urllib.error.HTTPError as e:
        print(f"[Discord] Webhook failed with HTTP {e.code}.")
        return False, e.code
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[Discord] Webhook request failed: {e}")
    return False, None


def send_discord_message(message, webhook_url=None, payload=None):
    """Discord Webhook으로 알림 메시지를 보낸다.

    Webhook URL이 없거나 Discord 호출이 실패해도 센서 저장 요청은 실패하면 안 된다.
    그래서 이 함수는 예외를 밖으로 던지지 않고 콘솔 로그와 False 반환으로만 알린다.

    webhook_url을 넘기면 장치별로 다른 채널에 보낼 수 있다.
    payload(dict)를 넘기면 임베드 등 Discord 본문을 그대로 보내고,
    Discord가 형식 오류(HTTP 400)로 거절하면 message를 일반 텍스트로 한 번 더 보낸다.
    """
    webhook_url = resolve_webhook_url(webhook_url)
    if not webhook_url:
        print(f"[Discord] {WEBHOOK_ENV_NAME} is not set. Alert skipped.")
        return False
    if not discord_webhook_url_is_allowed(webhook_url):
        print("[Discord] Refused non-Discord webhook URL.")
        return False

    if isinstance(payload, dict) and payload:
        ok, status = _post_webhook(webhook_url, payload)
        if ok or status != 400:
            return ok
        print("[Discord] Rich payload rejected; falling back to plain text.")

    ok, _ = _post_webhook(webhook_url, {"content": message})
    return ok
