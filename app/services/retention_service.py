"""원시 센서 데이터 보존 정책과 시간별 다운샘플링.

ESP32 한 대가 5초 주기로 보내면 하루 약 17,280행, 1년이면 630만 행이다.
라즈베리파이의 SD 카드는 이 증가를 오래 버티지 못하는데, 지금까지 삭제도
집계도 하지 않아 DB가 무한히 커지는 구조였다.

여기서는 두 단계로 처리한다.
1) 원시 행을 시간 단위로 집계해 sensor_data_hourly에 쌓는다(평균/최소/최대/건수).
2) 보존 기간이 지난 원시 행을 지운다. 집계는 남으므로 장기 추세는 유지된다.

집계를 먼저 하고 삭제를 나중에 하는 순서가 중요하다. 반대로 하면
아직 집계되지 않은 구간이 영구히 사라진다.
"""
import os
import sqlite3
from datetime import datetime, timedelta

try:
    from services import sensor_service
except ModuleNotFoundError:
    from app.services import sensor_service


# 원시 행을 보관하는 기본 기간이다.
# 대시보드의 상세 히스토리와 관수 감지가 원시 해상도를 쓰므로 넉넉히 잡는다.
DEFAULT_RAW_RETENTION_DAYS = 90
# 시간별 집계를 보관하는 기본 기간이다. 연 단위 추세를 보기 위해 길게 둔다.
DEFAULT_HOURLY_RETENTION_DAYS = 730


def _connect():
    # 센서 DB와 같은 파일을 사용한다. sensor_service의 경로 설정을 그대로 따른다.
    return sensor_service._connect()


def _env_int(name, default, minimum=1):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def raw_retention_days():
    return _env_int("SENSOR_RAW_RETENTION_DAYS", DEFAULT_RAW_RETENTION_DAYS)


def hourly_retention_days():
    return _env_int("SENSOR_HOURLY_RETENTION_DAYS", DEFAULT_HOURLY_RETENTION_DAYS)


def ensure_tables(conn=None):
    own = conn is None
    conn = conn or _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sensor_data_hourly (
                device_id         TEXT NOT NULL,
                bucket            TEXT NOT NULL,
                temperature_avg   REAL,
                temperature_min   REAL,
                temperature_max   REAL,
                humidity_avg      REAL,
                humidity_min      REAL,
                humidity_max      REAL,
                soil_moisture_avg REAL,
                soil_moisture_min REAL,
                soil_moisture_max REAL,
                light_avg         REAL,
                light_min         REAL,
                light_max         REAL,
                lux_samples       INTEGER NOT NULL DEFAULT 0,
                digital_samples   INTEGER NOT NULL DEFAULT 0,
                sample_count      INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (device_id, bucket)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sensor_hourly_bucket
            ON sensor_data_hourly(bucket)
            """
        )
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def _bucket_expr():
    # server_received_at을 시간 단위로 자른다. 형식이 'YYYY-MM-DD HH:00:00'이라
    # 문자열 비교만으로 시간 순서를 판단할 수 있다.
    return "strftime('%Y-%m-%d %H:00:00', server_received_at)"


def rollup_hourly(before=None, conn=None):
    """원시 행을 시간별 집계 테이블로 옮겨 담는다.

    이미 집계된 구간을 다시 계산해도 같은 결과가 나오도록 INSERT OR REPLACE를 쓴다.
    아직 진행 중인 현재 시간대는 집계하지 않는다. 그 시간의 데이터가 아직 다 들어오지
    않아, 지금 집계하면 평균이 실제와 달라지기 때문이다.
    """
    own = conn is None
    conn = conn or _connect()
    try:
        ensure_tables(conn)
        if before is None:
            # 현재 시간대의 시작 시각. 이 시각 이전 데이터만 집계 대상이다.
            before = datetime.now().strftime("%Y-%m-%d %H:00:00")
        elif isinstance(before, datetime):
            before = before.strftime("%Y-%m-%d %H:00:00")

        cursor = conn.execute(
            f"""
            INSERT OR REPLACE INTO sensor_data_hourly (
                device_id, bucket,
                temperature_avg, temperature_min, temperature_max,
                humidity_avg, humidity_min, humidity_max,
                soil_moisture_avg, soil_moisture_min, soil_moisture_max,
                light_avg, light_min, light_max,
                lux_samples, digital_samples, sample_count
            )
            SELECT
                device_id,
                {_bucket_expr()} AS bucket,
                AVG(temperature), MIN(temperature), MAX(temperature),
                AVG(humidity), MIN(humidity), MAX(humidity),
                AVG(soil_moisture), MIN(soil_moisture), MAX(soil_moisture),
                -- 조도는 단위가 섞이면 평균이 의미를 잃으므로 lux 행만 집계한다.
                AVG(CASE WHEN light_unit = 'lux' THEN light END),
                MIN(CASE WHEN light_unit = 'lux' THEN light END),
                MAX(CASE WHEN light_unit = 'lux' THEN light END),
                SUM(CASE WHEN light_unit = 'lux' THEN 1 ELSE 0 END),
                SUM(CASE WHEN light_unit = 'digital' THEN 1 ELSE 0 END),
                COUNT(*)
            FROM sensor_data
            WHERE server_received_at IS NOT NULL
              AND server_received_at != ''
              AND {_bucket_expr()} IS NOT NULL
              AND {_bucket_expr()} < ?
            GROUP BY device_id, bucket
            """,
            (before,),
        )
        if own:
            conn.commit()
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    finally:
        if own:
            conn.close()


def purge_raw(days=None, conn=None, require_rollup=True):
    """보존 기간이 지난 원시 행을 삭제한다.

    require_rollup이 True면 해당 시간대가 집계 테이블에 존재할 때만 지운다.
    집계되지 않은 구간을 삭제해 데이터를 영구히 잃는 사고를 막기 위한 안전장치다.
    """
    own = conn is None
    conn = conn or _connect()
    try:
        ensure_tables(conn)
        days = days if days is not None else raw_retention_days()
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        if require_rollup:
            cursor = conn.execute(
                f"""
                DELETE FROM sensor_data
                WHERE server_received_at IS NOT NULL
                  AND server_received_at != ''
                  AND server_received_at < ?
                  AND EXISTS (
                      SELECT 1 FROM sensor_data_hourly h
                      WHERE h.device_id = sensor_data.device_id
                        AND h.bucket = {_bucket_expr()}
                  )
                """,
                (cutoff,),
            )
        else:
            cursor = conn.execute(
                """
                DELETE FROM sensor_data
                WHERE server_received_at IS NOT NULL
                  AND server_received_at != ''
                  AND server_received_at < ?
                """,
                (cutoff,),
            )
        deleted = cursor.rowcount or 0
        if own:
            conn.commit()
        return deleted
    finally:
        if own:
            conn.close()


def purge_hourly(days=None, conn=None):
    own = conn is None
    conn = conn or _connect()
    try:
        ensure_tables(conn)
        days = days if days is not None else hourly_retention_days()
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:00:00")
        cursor = conn.execute(
            "DELETE FROM sensor_data_hourly WHERE bucket < ?", (cutoff,)
        )
        deleted = cursor.rowcount or 0
        if own:
            conn.commit()
        return deleted
    finally:
        if own:
            conn.close()


def run_maintenance(raw_days=None, hourly_days=None, vacuum=False):
    """집계 -> 원시 삭제 -> 오래된 집계 삭제 순으로 한 번에 수행한다.

    순서를 바꾸면 안 된다. 집계 전에 삭제하면 그 구간은 복구할 수 없다.
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ensure_tables(conn)
        rolled = rollup_hourly(conn=conn)
        removed_raw = purge_raw(raw_days, conn=conn)
        removed_hourly = purge_hourly(hourly_days, conn=conn)
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        print(f"[Retention] Maintenance failed: {e}")
        raise
    finally:
        conn.close()

    result = {
        "rolled_up_buckets": rolled,
        "deleted_raw_rows": removed_raw,
        "deleted_hourly_rows": removed_hourly,
        "raw_retention_days": raw_days if raw_days is not None else raw_retention_days(),
        "hourly_retention_days": (
            hourly_days if hourly_days is not None else hourly_retention_days()
        ),
    }

    if vacuum and removed_raw:
        # DELETE만으로는 SQLite 파일 크기가 줄지 않는다.
        # VACUUM은 파일을 통째로 다시 쓰므로 명시적으로 요청했을 때만 실행한다.
        vacuum_conn = _connect()
        try:
            vacuum_conn.isolation_level = None
            vacuum_conn.execute("VACUUM")
            result["vacuumed"] = True
        except sqlite3.Error as e:
            print(f"[Retention] VACUUM skipped: {e}")
            result["vacuumed"] = False
        finally:
            vacuum_conn.close()
    else:
        result["vacuumed"] = False

    return result


def storage_stats():
    """대시보드가 보여줄 저장 현황이다."""
    conn = _connect()
    try:
        ensure_tables(conn)
        raw_count = conn.execute("SELECT COUNT(*) FROM sensor_data").fetchone()[0]
        hourly_count = conn.execute(
            "SELECT COUNT(*) FROM sensor_data_hourly"
        ).fetchone()[0]
        oldest = conn.execute(
            "SELECT MIN(server_received_at) FROM sensor_data"
            " WHERE server_received_at IS NOT NULL AND server_received_at != ''"
        ).fetchone()[0]
        newest = conn.execute(
            "SELECT MAX(server_received_at) FROM sensor_data"
            " WHERE server_received_at IS NOT NULL AND server_received_at != ''"
        ).fetchone()[0]
    except sqlite3.Error as e:
        print(f"[Retention] Failed to read storage stats: {e}")
        return {
            "raw_rows": 0, "hourly_rows": 0, "oldest_raw_at": None,
            "newest_raw_at": None, "database_bytes": 0,
            "raw_retention_days": raw_retention_days(),
            "hourly_retention_days": hourly_retention_days(),
        }
    finally:
        conn.close()

    try:
        # WAL 파일까지 합쳐야 실제 디스크 사용량에 가깝다.
        size = 0
        for suffix in ("", "-wal", "-shm"):
            path = sensor_service.DB_FILE + suffix
            if os.path.exists(path):
                size += os.path.getsize(path)
    except OSError:
        size = 0

    return {
        "raw_rows": int(raw_count or 0),
        "hourly_rows": int(hourly_count or 0),
        "oldest_raw_at": oldest,
        "newest_raw_at": newest,
        "database_bytes": size,
        "raw_retention_days": raw_retention_days(),
        "hourly_retention_days": hourly_retention_days(),
    }
