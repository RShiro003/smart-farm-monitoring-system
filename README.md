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
