from flask import Blueprint, render_template, request, jsonify
from datetime import datetime, timedelta
from collections import defaultdict

# 대시보드 전용 라우트 모듈이다.
# 이 파일은 화면 렌더링과 브라우저가 주기적으로 호출하는 조회 API만 담당하고,
# ESP32가 데이터를 보내는 /api/sensor POST는 app/routes/sensor.py에서 처리한다.
try:
    from services.sensor_service import (
        filter_sensor_data,
        latest_sensor_record,
        list_device_ids,
        load_sensor_data,
        normalize_device_filter,
        parse_record_time,
        sensor_data_mtime,
    )
except ModuleNotFoundError:
    from app.services.sensor_service import (
        filter_sensor_data,
        latest_sensor_record,
        list_device_ids,
        load_sensor_data,
        normalize_device_filter,
        parse_record_time,
        sensor_data_mtime,
    )

dashboard_bp = Blueprint("dashboard", __name__)


def _selected_device_id():
    # URL 쿼리의 device_id를 대시보드 전체 조회 기준으로 사용한다.
    # 값이 없거나 all이면 전체 장치, 특정 값이면 해당 ESP32 노드만 필터링한다.
    return normalize_device_filter(request.args.get("device_id"))


def _load_data(device_id=None):
    # 대시보드의 여러 API가 같은 방식으로 "DB 전체 조회 후 device_id 필터"를 사용하므로
    # 중복을 줄이고 필터 규칙을 한 곳에 모은다.
    return filter_sensor_data(load_sensor_data(), device_id)


def _device_options(all_data, selected_device_id):
    # 저장된 데이터에 존재하는 device_id를 드롭다운 선택지로 만든다.
    # 사용자가 URL로 아직 데이터가 없는 device_id를 직접 지정한 경우에도 선택 상태가 사라지지 않게 추가한다.
    devices = list_device_ids(all_data)
    if selected_device_id and selected_device_id not in devices:
        devices.append(selected_device_id)
    return sorted(devices)


def _parse_time(row):
    # 시간 파싱 정책은 sensor_service.parse_record_time()에 있다.
    # 대시보드에서는 통계/그래프/검색에 같은 기준을 적용하기 위해 이 래퍼를 사용한다.
    return parse_record_time(row)


def _avg(rows, key, alt_key=None):
    # 기간별 평균과 차트 집계에서 공통으로 사용하는 평균 계산이다.
    # 조도는 예전/실제 노드 데이터가 light 또는 light_digital 중 하나만 가질 수 있어 alt_key를 지원한다.
    vals = []
    for r in rows:
        v = r.get(key)
        if v is None and alt_key:
            v = r.get(alt_key)
        if v is not None:
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
    return round(sum(vals) / len(vals), 1) if vals else None


def _calculate_averages(data, period):
    # 브라우저의 "기간별 평균" 카드가 호출하는 계산 로직이다.
    # 모든 row를 매번 내려보내지 않고 서버에서 필요한 기간만 필터링해 평균과 건수를 반환한다.
    now = datetime.now()
    delta = {
        "daily": timedelta(days=1),
        "weekly": timedelta(weeks=1),
        "monthly": timedelta(days=30),
    }
    cutoff = now - delta.get(period, timedelta(days=1))

    filtered = []
    for r in data:
        t = _parse_time(r)
        if t and t >= cutoff:
            filtered.append(r)

    return {
        "temperature": _avg(filtered, "temperature"),
        "humidity": _avg(filtered, "humidity"),
        "soil_moisture": _avg(filtered, "soil_moisture"),
        "light": _avg(filtered, "light", "light_digital"),
        "count": len(filtered),
    }


def _aggregate_chart(data, period):
    # Chart.js에 넣기 쉬운 형태로 센서 row를 시간 단위 그룹으로 묶는다.
    # hourly/daily/weekly/monthly 버튼은 같은 API를 period 값만 바꿔 호출한다.
    now = datetime.now()

    if period == "hourly":
        cutoff = now - timedelta(hours=24)
        def key_fn(t): return t.strftime("%m/%d %H:00")
    elif period == "daily":
        cutoff = now - timedelta(days=7)
        def key_fn(t): return t.strftime("%m/%d")
    elif period == "weekly":
        cutoff = now - timedelta(weeks=8)
        def key_fn(t):
            start = t - timedelta(days=t.weekday())
            return start.strftime("%m/%d~")
    else:  # monthly
        cutoff = now - timedelta(days=365)
        def key_fn(t): return t.strftime("%Y/%m")

    groups = defaultdict(list)
    for row in data:
        t = _parse_time(row)
        if t and t >= cutoff:
            groups[key_fn(t)].append(row)

    # labels와 각 센서 배열의 인덱스가 서로 맞아야 Chart.js에서 같은 시간축에 그릴 수 있다.
    labels = sorted(groups.keys())
    return {
        "labels": labels,
        "temperature": [_avg(groups[l], "temperature") for l in labels],
        "humidity": [_avg(groups[l], "humidity") for l in labels],
        "soil_moisture": [_avg(groups[l], "soil_moisture") for l in labels],
        "light": [_avg(groups[l], "light", "light_digital") for l in labels],
    }


@dashboard_bp.route("/dashboard")
def dashboard():
    # 최초 화면 렌더링 시에는 최신 row와 장치 목록만 템플릿에 넣는다.
    # 이후 현재값/평균/그래프/히스토리는 JavaScript가 API를 호출해 갱신한다.
    selected_device_id = _selected_device_id()
    all_data = load_sensor_data()
    data = filter_sensor_data(all_data, selected_device_id)
    latest = latest_sensor_record(data)
    return render_template(
        "index.html",
        latest=latest,
        devices=_device_options(all_data, selected_device_id),
        current_device_id=selected_device_id,
    )


@dashboard_bp.route("/api/dashboard/stats")
def dashboard_stats():
    # 기간별 평균 카드용 API.
    # device_id 필터와 period 쿼리를 함께 반영해 현재 선택된 장치 기준의 요약값을 반환한다.
    period = request.args.get("period", "daily")
    data = _load_data(_selected_device_id())
    return jsonify(_calculate_averages(data, period))


@dashboard_bp.route("/api/dashboard/chart")
def dashboard_chart():
    # 데이터 추이 그래프용 API.
    # 서버에서 시간 버킷별 평균을 만든 뒤 브라우저는 반환된 labels/data만 Chart.js에 반영한다.
    period = request.args.get("period", "hourly")
    data = _load_data(_selected_device_id())
    return jsonify(_aggregate_chart(data, period))


@dashboard_bp.route("/api/dashboard/latest")
def dashboard_latest():
    # 최신 센서값만 필요한 클라이언트를 위한 API다.
    # 현재 템플릿에서는 /api/sensor 전체 목록의 마지막 row를 쓰지만, 단일 최신값 조회용으로 유지한다.
    data = _load_data(_selected_device_id())
    return jsonify(latest_sensor_record(data))


@dashboard_bp.route("/api/dashboard/history")
def dashboard_history():
    # 전체 기록 테이블용 API.
    # 브라우저에서 페이지/날짜/시간 필터를 넘기면 서버가 필터링과 페이지네이션을 수행한다.
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = max(1, int(request.args.get("per_page", 10)))
    except ValueError:
        page, per_page = 1, 10

    date_str  = request.args.get("date", "").strip()       # YYYY-MM-DD
    time_from = request.args.get("time_from", "").strip()  # HH:MM
    time_to   = request.args.get("time_to",   "").strip()  # HH:MM

    data = list(reversed(_load_data(_selected_device_id())))  # newest first

    filtered = []
    has_filter = bool(date_str or time_from or time_to)
    for row in data:
        t = _parse_time(row)
        if t is None:
            # 시간 정보가 없는 과거/오류 row는 날짜 검색 조건이 없을 때만 보여준다.
            # 검색 조건이 있을 때 포함하면 사용자가 의도한 기간 밖의 데이터가 섞일 수 있다.
            if not has_filter:
                filtered.append(row)
            continue

        if date_str and t.strftime("%Y-%m-%d") != date_str:
            continue
        if time_from:
            try:
                tf = datetime.strptime(time_from, "%H:%M").time()
                if t.time() < tf:
                    continue
            except ValueError:
                pass
        if time_to:
            try:
                tt = datetime.strptime(time_to, "%H:%M").time()
                if t.time() > tt:
                    continue
            except ValueError:
                pass
        filtered.append(row)

    total  = len(filtered)
    pages  = max(1, (total + per_page - 1) // per_page)
    page   = min(page, pages)
    start  = (page - 1) * per_page
    items  = filtered[start:start + per_page]

    return jsonify({
        "items": items,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "total": total,
    })


@dashboard_bp.route("/api/dashboard/mtime")
def dashboard_mtime():
    # 브라우저 폴링용 변경 감지 API.
    # MAX(id)가 바뀌면 새 센서 row가 들어온 것이므로 JS가 현재값/그래프/히스토리를 다시 불러온다.
    return jsonify({"mtime": sensor_data_mtime()})
