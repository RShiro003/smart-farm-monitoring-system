"""API 키 인증.

지금까지 모든 엔드포인트가 무인증이었다. 라즈베리파이를 포트포워딩하거나
공용 Wi-Fi에 두는 순간 누구나 센서 데이터를 위조하고 임계값을 바꾸거나
작물 프로필을 지울 수 있다.

설계 원칙 두 가지:
1) 키를 설정하지 않으면 인증은 완전히 비활성이다. 기존 설치 환경이
   업데이트만으로 잠겨서 ESP32가 데이터를 못 보내는 사고를 막기 위해서다.
2) 키를 설정하면 쓰기 요청을 막는다. 조회는 기본적으로 열어 두되
   SMART_FARM_PROTECT_READS=true로 함께 잠글 수 있다.
"""
import hashlib
import hmac
import json
import os
import time

from flask import jsonify, request


# ESP32는 헤더를 붙이고, 브라우저는 로그인 API가 발급한 HttpOnly 쿠키를 쓴다.
# 쿼리스트링 키는 서버 로그·브라우저 기록·리퍼러에 남으므로 허용하지 않는다.
API_KEY_HEADER = "X-API-Key"
API_KEY_ENV = "SMART_FARM_API_KEY"
PROTECT_READS_ENV = "SMART_FARM_PROTECT_READS"
DEVICE_KEYS_ENV = "SMART_FARM_DEVICE_KEYS"
AUTH_COOKIE_NAME = "smart_farm_session"
_SESSION_PURPOSE = b"smart-farm-dashboard-session-v1"
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60
ALWAYS_AUTH_PREFIXES = (
    "/api/alert-settings",
    "/api/maintenance",
    "/api/export",
)

# 인증 없이도 항상 열려 있어야 하는 경로다.
# 상태 확인은 감시 도구가 호출하고, 정적 리소스는 화면 표시에 필요하다.
ALWAYS_OPEN_PATHS = (
    "/",
    "/dashboard",
    "/api/status",
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/logout",
)


def configured_key():
    key = os.environ.get(API_KEY_ENV, "")
    return key.strip()


def configured_device_keys():
    raw = os.environ.get(DEVICE_KEYS_ENV, "").strip()
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(values, dict):
        return {}
    return {
        str(device_id): key.strip()
        for device_id, key in values.items()
        if isinstance(key, str) and key.strip()
    }


def auth_enabled():
    return bool(configured_key())


def protect_reads():
    raw = os.environ.get(PROTECT_READS_ENV, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _presented_key():
    header = request.headers.get(API_KEY_HEADER)
    if header and header.strip():
        return header.strip()

    # Authorization: Bearer <key> 형태도 받아 준다. 일반적인 API 클라이언트 관행이다.
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()
        if candidate:
            return candidate

    return None


def key_is_valid(candidate):
    expected = configured_key()
    if not expected or not candidate:
        return False
    # 타이밍 공격을 피하기 위해 길이에 무관한 비교를 쓴다.
    return hmac.compare_digest(candidate, expected)


def session_cookie_value():
    """현재 API 키로 서명한 브라우저 세션 토큰을 만든다.

    원본 API 키를 쿠키에 넣지 않으므로 개발자 도구나 쿠키 저장소가 노출돼도
    ESP32가 사용하는 장기 키 자체가 그대로 유출되지는 않는다.
    """
    key = configured_key()
    if not key:
        return None
    issued_at = str(int(time.time()))
    payload = _SESSION_PURPOSE + b":" + issued_at.encode("ascii")
    signature = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{issued_at}.{signature}"


def session_cookie_is_valid(candidate):
    key = configured_key()
    if not key or not isinstance(candidate, str):
        return False
    try:
        issued_raw, signature = candidate.split(".", 1)
        issued_at = int(issued_raw)
    except (TypeError, ValueError):
        return False
    age = int(time.time()) - issued_at
    if age < -300 or age > SESSION_MAX_AGE_SECONDS:
        return False
    payload = _SESSION_PURPOSE + b":" + issued_raw.encode("ascii")
    expected = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def request_is_authorized():
    return (
        key_is_valid(_presented_key())
        or session_cookie_is_valid(request.cookies.get(AUTH_COOKIE_NAME))
    )


def _device_request_id():
    if request.path == "/api/sensor" and request.method == "POST":
        payload = request.get_json(silent=True)
        return payload.get("device_id") if isinstance(payload, dict) else None
    if request.path == "/api/thresholds" and request.method == "GET":
        return request.args.get("device_id")
    return None


def _is_device_endpoint():
    return (
        (request.path == "/api/sensor" and request.method == "POST")
        or (request.path == "/api/thresholds" and request.method == "GET")
    )


def _device_request_is_authorized():
    device_id = _device_request_id()
    expected = configured_device_keys().get(device_id)
    candidate = _presented_key()
    return bool(expected and candidate and hmac.compare_digest(candidate, expected))


def _requires_auth():
    if not auth_enabled():
        return False
    if request.method == "OPTIONS":
        return False
    if request.path in ALWAYS_OPEN_PATHS:
        return False
    if request.path.startswith(ALWAYS_AUTH_PREFIXES):
        return True
    # 대시보드 화면 자체는 읽기 보호를 켰을 때만 막는다.
    if request.method in ("GET", "HEAD"):
        return protect_reads()
    return True


def install(app):
    """Flask 앱에 인증 훅을 건다.

    라우트마다 데코레이터를 붙이지 않고 before_request로 한 번에 처리한다.
    새 엔드포인트를 추가할 때 인증을 빠뜨리는 실수를 구조적으로 막기 위해서다.
    """

    @app.before_request
    def _check_api_key():
        if _is_device_endpoint() and (auth_enabled() or configured_device_keys()):
            # 장치 키는 자신의 센서 수집/임계값 조회에서만 유효하다. 관리자
            # 세션이나 전역 키도 유지보수 호환을 위해 허용한다.
            if _device_request_is_authorized() or request_is_authorized():
                return None
            return (
                jsonify({
                    "error": "Unauthorized device",
                    "detail": f"Send the device key in the {API_KEY_HEADER} header.",
                }),
                401,
            )
        if not _requires_auth():
            return None
        if request_is_authorized():
            return None
        return (
            jsonify({
                "error": "Unauthorized",
                "detail": f"Send the API key in the {API_KEY_HEADER} header.",
            }),
            401,
        )

    return app


def auth_status():
    """대시보드가 인증 상태를 표시하기 위해 읽는 값이다. 키 자체는 노출하지 않는다."""
    return {
        "enabled": auth_enabled(),
        "protect_reads": protect_reads() if auth_enabled() else False,
        "authenticated": request_is_authorized() if auth_enabled() else True,
        "header": API_KEY_HEADER,
    }
