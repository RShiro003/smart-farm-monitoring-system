# Smart Farm Monitoring System

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
