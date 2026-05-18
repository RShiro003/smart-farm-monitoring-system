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

To add another ESP32 board, change only the `DEVICE_ID` constant near the top of
that board's `src/main.cpp` before flashing:

```cpp
const char* DEVICE_ID = "esp32_01";
```

Use a different value for each board, for example `esp32_01`, `esp32_02`, and
`esp32_greenhouse_north`. The dashboard and `/api/sensor?device_id=...` filter
data by this value.
