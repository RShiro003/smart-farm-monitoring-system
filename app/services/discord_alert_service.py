import json
import os
import urllib.error
import urllib.request


WEBHOOK_ENV_NAME = "DISCORD_WEBHOOK_URL"


def send_discord_message(message):
    """Discord Webhook으로 알림 메시지를 보낸다.

    Webhook URL이 없거나 Discord 호출이 실패해도 센서 저장 요청은 실패하면 안 된다.
    그래서 이 함수는 예외를 밖으로 던지지 않고 콘솔 로그와 False 반환으로만 알린다.
    """
    webhook_url = os.environ.get(WEBHOOK_ENV_NAME, "").strip()
    if not webhook_url:
        print(f"[Discord] {WEBHOOK_ENV_NAME} is not set. Alert skipped.")
        return False

    payload = json.dumps({"content": message}, ensure_ascii=False).encode("utf-8")
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
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[Discord] Webhook request failed: {e}")
    return False
