# Smart Farm Monitoring System

This project is monitoring-only. ESP32 nodes collect sensor readings, the server
checks thresholds, Discord delivers alerts, and the dashboard keeps the history.
It does not expose APIs or firmware logic for remotely operating pumps, fans,
lights, relays, or other physical equipment.

## Sensor Data Storage

Sensor API routes live in `app/routes/sensor.py` and are registered from
`app/main.py` as a Flask Blueprint.

The Flask server stores sensor rows in SQLite at `app/data/sensor_data.db`.
Every new POST to `/api/sensor` must include a non-empty `device_id`; older rows
without `device_id` are treated as `legacy` when they are read.

`app/data/sensor_data.json` is a legacy import source for old JSON-based data.
It is not the runtime storage target for new sensor readings. New readings are
inserted into the `sensor_data` SQLite table, and only the columns defined in
`app/services/sensor_service.py` are persisted.

## Historical Watering Detection

Run watering detection on existing SQLite sensor records explicitly:

```sh
curl -X POST 'http://localhost:5000/api/watering/backfill?device_id=esp32_01'
```

Omit `device_id` to process every non-empty device ID stored in `sensor_data`
independently (including `legacy` when it has valid receive times). An explicitly
empty device ID returns HTTP 400; an unknown device returns zero counts.

The JSON response contains `scanned_samples` (all selected sensor rows, including
unusable samples), `created_events` (new events), and `existing_events` (distinct
existing events matched by duplicate/cooldown checks). Repeated candidate windows
for an event created during the same run do not increase `existing_events`.

Backfill reads each device in `server_received_at ASC, id ASC` order and applies
the live detector to the recent detection window with the same sample limit,
cooldown, and event refinement rules. Existing events on either side of a
historical candidate prevent duplicates inside the cooldown. Re-running the
request is safe; original sensor rows are never changed or deleted. Samples with
missing/invalid `server_received_at` cannot be evaluated.

The request runs synchronously with a bounded sample buffer and short event write
transactions. It scans the selected history only when called, never at startup.
Committed events are immediately available from `/api/watering?device_id=...`;
the dashboard's recent watering section displays them on reload or its next
15-second refresh, subject to its existing latest-20-event limit.

## Multiple ESP32 Sensor Nodes

ESP32 Wi-Fi credentials, Raspberry Pi server URLs, and `DEVICE_ID` values are
kept out of `src/main.cpp`. For each ESP32 PlatformIO project, copy the example
file and fill in local values before flashing:

```text
esp32-dummy-node/include/secrets.example.h -> esp32-dummy-node/include/secrets.h
esp32-real-sensor-node/include/secrets.example.h -> esp32-real-sensor-node/include/secrets.h
```

`include/secrets.h` is ignored by Git, so keep the real `WIFI_SSID`,
`WIFI_PASSWORD`, `SERVER_URL`, `THRESHOLDS_URL` if present, and `DEVICE_ID`
there. Use a different `DEVICE_ID` for each board, for example `esp32_01`,
`esp32_02`, and `esp32_greenhouse_north`. The dashboard and
`/api/sensor?device_id=...` filter data by this value.

## Light Units

`light` can hold either a real illuminance reading or a comparator module's
digital output, and the two cannot be compared against the same threshold.
Every reading therefore carries a `light_unit`:

| `light_unit` | Meaning | Light alerts |
|---|---|---|
| `lux` | Measured illuminance. `esp32-dummy-node` sends this. | Checked against `light_min` / `light_max` |
| `digital` | Comparator output, `0` or `1` (LM393-style). `esp32-real-sensor-node` sends this. | Skipped — a 0/1 value can never fall inside a lux range |

Nodes that omit `light_unit` are classified on arrival: if `light_digital` is
present and equals `light`, the row is treated as `digital`, otherwise as
`lux`. Existing rows are classified once by the same rule when the `light_unit`
column is added to an older database.

The default light range is the full range the sensor route accepts
(0–200000 lux), so light alerts stay quiet until someone sets a real range or
applies a crop profile. Databases still holding the old `0`–`100` placeholder
are moved to this default once, tracked by `PRAGMA user_version`; a range you
set yourself afterwards is never rewritten.

The dashboard renders `digital` readings as 밝음/어두움 with the raw value
beneath, because polarity differs between modules, and shows 측정 방식 다름
instead of a 적정/주의/위험 badge for those devices.

## Crop Profiles vs Device Thresholds

These are two different things and the dashboard now says so explicitly:

- **Crop profile** (`crop_profiles`) — recommended ranges for a crop. Drives the
  guide panel and the chart reference lines only.
- **Device thresholds** (`threshold_settings`) — what the ESP32 uses for its
  status LED and what `alert_service` uses for Discord alerts.

Selecting a crop changes only the guide. To make a device actually judge by the
crop's ranges, use **작물 기준 적용** in the threshold panel, which calls
`POST /api/thresholds/from-crop` with `{device_id, crop_id}`. Soil calibration
(`soil_dry_raw` / `soil_wet_raw`) is hardware-specific and is never overwritten
by a crop. The panel states whether the saved thresholds currently match the
selected crop.

For a device that reports `light_unit=digital`, crop lux thresholds are not
comparable. Applying a crop therefore stores the safe digital range `0..1` for
that device, and the digital-sensor firmware skips lux-based LED judgement.

## Alerts and Offline Detection

`alert_service` writes every threshold crossing and recovery to `event_log` and
sends it to Discord. Those records are readable from the dashboard's 알림 기록
section and from `GET /api/events` (the existing
`GET /api/dashboard/events` path remains as a compatibility alias). Both paths
accept `device_id`, `status`, `metric`, legacy single-day `date`, date-range
`date_from` / `date_to`, and `time_from` / `time_to` filters. The dashboard uses
the range fields so older months can be queried directly instead of paging back
from the newest event.

Device outages cannot be detected while handling `/api/sensor`, because an
outage means that request never arrives. A background watchdog thread instead
polls last-seen times and records `device_offline` / recovery events. It starts
on the first HTTP request so Flask's reloader does not run two copies.

## Device Registry

Devices used to exist only as a `device_id` that had appeared in sensor data, so a
new board was invisible until it sent its first reading, and the dashboard could
only show identifiers. `GET/POST /api/devices` stores a label, location and note
per device, and `DELETE /api/devices/<id>` removes only that metadata — sensor
rows are never deleted, so the device reappears in the list as unregistered.
The list merges registered devices with any device id seen in sensor data, so
existing installations keep working without registering anything.

## Alert Configuration

Per-metric on/off, the re-alert cooldown and the Discord webhook are stored in
the `alert_settings` table and editable from the dashboard, instead of being
code constants and environment variables that needed a restart.

Resolution order is device row → global row (`device_id = __global__`) → built-in
default, so you can set one default and override a few devices.
`DELETE /api/alert-settings/<id>` drops a device override. Nothing configured
means every alert is on and `ALERT_COOLDOWN_MINUTES` applies, matching the
previous behaviour. A stored webhook lets one device alert a different channel.

### Alert confirmation and severity

The alert settings panel can require both a minimum number of consecutive
abnormal readings and a minimum abnormal duration before an event is recorded.
Recovery can also require consecutive normal readings. The defaults remain one
reading and zero seconds for backward compatibility; increase them to suppress
short sensor spikes.

Abnormal values are labelled `warning` or `danger`. Danger is calculated from
how far the value lies outside the normal range relative to that range's width.
The percentage is configurable, and danger events can bypass confirmation delay
so a large excursion is delivered immediately. Event history and CSV exports
include the severity.

### Daily and weekly Discord summaries

Daily and weekly summaries can be enabled globally or per device in the alert
settings panel. Configure the local send hour and the weekday for weekly
summaries. The watchdog queues each completed period only once in the durable
notification outbox. A summary includes sample and sensor-error counts,
temperature/humidity/soil/light average and range, warning/danger/recovery event
counts, and offline events. Failed summary delivery follows the same retry policy
as immediate alerts.

## Manual Work Log

`GET/POST /api/work-logs` and `DELETE /api/work-logs/<id>` manage operator-entered
records for watering, ventilation, fertilizing, crop inspection, sensor
maintenance, and other work. This is an observation log only and never controls
equipment. The dashboard lists records for the selected device and draws their
times as purple vertical annotations on all sensor charts.

## CSV Export

`GET /api/export/sensor.csv` and `GET /api/export/events.csv` accept the same
filters as the dashboard tables (`device_id`, `date`, `time_from`, `time_to`,
plus `date_from`/`date_to` and `status`/`metric` for events), so a download matches
what you were looking at. Rows are streamed one at a time rather than collected in memory, because a
wide date range at 5-second sampling is hundreds of thousands of rows. Output
carries a UTF-8 BOM so Excel does not mangle the Korean headers, and each file
includes the device label next to the id. All matching stored rows are exported;
there is no silent 200,000-row cutoff. Large exports can take time, so use date
and device filters when possible. Database/transfer errors interrupt the stream
instead of being silently treated as a completed partial export.

Time filters apply to the recorded local clock on each selected day. An end
time of `10:30` includes the entire minute through `10:30:59`; API callers may
also supply `HH:MM:SS`. A single time bound works with or without a date bound.
Invalid or reversed ranges match no rows. An overnight interval must be queried
as two clock intervals. Click Search after changing filters; CSV uses the applied
table filters. Raw sensor rows already removed by retention cannot be recovered
by CSV export; hourly rollups are not substituted for raw measurements.

## Retention and Downsampling

One node at 5-second sampling writes ~17,000 rows/day, ~6.3M/year, and nothing
used to delete or summarise them.

`POST /api/maintenance/rollup` aggregates completed hours into
`sensor_data_hourly` (avg/min/max plus lux and digital sample counts; the
in-progress hour is skipped so its average is not computed from partial data).
`POST /api/maintenance/retention` runs rollup first and only then deletes raw
rows past the retention window — and only for hours that are present in the
aggregate table, so an unaggregated gap can never be deleted. `GET
/api/maintenance/storage` reports row counts, database size and the active
policy. Pass `{"vacuum": true}` to reclaim file space, since `DELETE` alone does
not shrink a SQLite file.

These endpoints run on demand; nothing is deleted automatically. Schedule them
with cron if you want unattended cleanup.

## API Key Authentication

Every endpoint used to be unauthenticated. Set `SMART_FARM_API_KEY` to require a
key; leave it unset and authentication is entirely disabled, so existing
installations do not lock out their nodes on upgrade.

When enabled, write requests require authentication and reads stay
open unless `SMART_FARM_PROTECT_READS=true`. `/api/status` is always reachable
for health checks, but omits the latest sensor row from unauthenticated responses
when reads are protected. API clients and nodes send the key as `X-API-Key` or
`Authorization: Bearer <key>`. Query-string keys are intentionally rejected
because URLs are commonly retained in browser history, proxy logs and referrer
headers.

The dashboard asks for the key and exchanges it for a 12-hour HttpOnly,
SameSite=Strict session cookie; it never stores the original key in localStorage.
Put the same value in each node's `secrets.h` (`API_KEY`); an empty string there
means no header is sent. The check is installed as a single `before_request`
hook so a newly added route cannot accidentally skip it. API keys sent over the
example plain-HTTP URLs are visible to other machines on that network, so use a
trusted isolated LAN or terminate HTTPS in a reverse proxy for untrusted links.

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | unset | Discord webhook. Alerts are skipped when unset. |
| `ALERT_COOLDOWN_MINUTES` | `10` | Minimum gap before repeating an alert that is still active. |
| `DEVICE_OFFLINE_SECONDS` | `120` | Silence after which a device counts as offline. |
| `DEVICE_OFFLINE_CHECK_SECONDS` | `30` | Watchdog polling interval. |
| `SMART_FARM_SENSOR_DB_FILE` | `app/data/sensor_data.db` | Sensor readings, `event_log`, `alert_state`, `alert_settings`, hourly rollups, growth and watering tables. |
| `SMART_FARM_DB_FILE` | `app/data/smart_farm.db` | Thresholds, crop profiles and the device registry. |
| `SMART_FARM_API_KEY` | unset | API key. Authentication is disabled while unset. |
| `SMART_FARM_DEVICE_KEYS` | unset | JSON object mapping each `device_id` to its own node key. Node keys cannot access admin APIs. |
| `SMART_FARM_PROTECT_READS` | `false` | Also require the key for GET requests. |
| `SMART_FARM_MAX_REQUEST_BYTES` | `65536` | Maximum HTTP request body size. |
| `SMART_FARM_BEHIND_PROXY` | `false` | Trust one local reverse proxy's forwarded client/protocol headers. Enable only when Waitress is bound to localhost behind that proxy. |
| `SENSOR_RAW_RETENTION_DAYS` | `90` | How long raw sensor rows are kept. |
| `SENSOR_HOURLY_RETENTION_DAYS` | `730` | How long hourly aggregates are kept. |

Watering detection has its own `WATERING_*` variables, listed at the top of
`app/services/cultivation_service.py`.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` / `POST` | `/api/sensor` | Read / ingest sensor rows |
| `GET` | `/api/status` | Health check with latest row |
| `GET` / `POST` / `PUT` | `/api/thresholds` | Per-device thresholds |
| `POST` | `/api/thresholds/from-crop` | Copy a crop's ranges into a device's thresholds |
| `GET` / `POST` | `/api/crops` | List / create crop profiles |
| `PUT` / `DELETE` | `/api/crops/<id>` | Update / delete a crop profile |
| `GET` / `PUT` | `/api/crops/device` | Crop assigned to a device |
| `GET` / `POST` | `/api/growth` | Growth records |
| `GET` | `/api/watering` | Detected watering events |
| `POST` | `/api/watering/backfill` | Re-run detection over stored history |
| `GET` | `/api/analysis/daily` | Daily environment summary |
| `GET` | `/api/dashboard/latest` | Latest reading |
| `GET` | `/api/dashboard/stats` | Period averages; lux-only light aggregation |
| `GET` | `/api/dashboard/chart` | Bucketed chart series; digital light excluded from lux series |
| `GET` | `/api/dashboard/history` | Paged reading history |
| `GET` | `/api/events` | Paged alert history (`device_id`, `status`, `metric`, date range/time filters) |
| `GET` | `/api/dashboard/events` | Compatibility alias for `/api/events` |
| `GET` | `/api/dashboard/device-status` | Per-device online/offline state |
| `GET` | `/api/dashboard/devices` | Known device IDs |
| `GET` | `/api/dashboard/mtime` | Change token for dashboard polling |
| `GET` / `POST` / `PUT` | `/api/devices` | Device registry (label, location, note) |
| `DELETE` | `/api/devices/<id>` | Remove device metadata (keeps sensor rows) |
| `GET` / `POST` / `PUT` | `/api/alert-settings` | Per-device or global alert configuration |
| `DELETE` | `/api/alert-settings/<id>` | Drop a device override |
| `GET` / `POST` | `/api/work-logs` | List or create operator work records |
| `DELETE` | `/api/work-logs/<id>` | Delete a work record |
| `GET` | `/api/export/sensor.csv` | Stream sensor rows as CSV |
| `GET` | `/api/export/events.csv` | Stream alert history as CSV |
| `GET` | `/api/maintenance/storage` | Row counts, DB size, retention policy |
| `POST` | `/api/maintenance/rollup` | Aggregate completed hours (no deletion) |
| `POST` | `/api/maintenance/retention` | Roll up, then delete expired raw rows |
| `GET` | `/api/auth/status` | Whether the API key is required (never returns the key) |

## Tests

```sh
python -m unittest discover -s tests
```

Test modules pin the service `DB_FILE` globals to temporary databases, so runs
never touch `app/data/`.

## Deployment

`app.run()` starts Werkzeug's development server, which is not built for
sustained traffic. On this Windows machine it stops answering after roughly
25–30 rapid requests — the handler still runs and logs, but the response never
reaches the client. This reproduces on an unmodified checkout, with both
`threaded=True` and `threaded=False`, and with `curl` as well as Python clients,
so it is a property of the dev server rather than of any one feature.

Run it behind a real WSGI server instead:

```sh
pip install -r requirements.txt
# repo 루트에서 실행한다. app.main:app 은 모듈 수준 Flask 객체다.
waitress-serve --host=0.0.0.0 --port=5000 app.main:app
```

The background jobs (offline watchdog and notification retry worker) start on
the first request. `/api/status` reports database, disk, retry queue, and
watchdog state. Monitor that endpoint from a different machine: an in-process
watchdog cannot report a Raspberry Pi power, process, router, or site outage.

For Linux/Raspberry Pi, `deploy/smart-farm.service` is a hardened systemd unit
template. Copy `.env.example` to `/etc/smart-farm.env`, replace every placeholder,
restrict it to the service account, and place an HTTPS reverse proxy or VPN in
front of Waitress. Do not expose port 5000 directly to the Internet.

Sensor nodes may send a `sample_id`; repeated submissions with the same
`device_id` and `sample_id` are stored only once. The real sensor firmware keeps
a bounded in-memory retry queue during Wi-Fi/server outages and submits DHT
failures as nullable readings with `sensor_errors`, preserving soil and light
measurements.
