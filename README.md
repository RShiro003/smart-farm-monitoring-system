# Smart Farm Monitoring System

## Multiple ESP32 Sensor Nodes

The Flask server stores sensor rows in `app/data/sensor_data.json`. Every new
POST to `/api/sensor` must include a non-empty `device_id`; older rows without
`device_id` are treated as `legacy` when they are read.

To add another ESP32 board, change only the `DEVICE_ID` constant near the top of
that board's `src/main.cpp` before flashing:

```cpp
const char* DEVICE_ID = "esp32_01";
```

Use a different value for each board, for example `esp32_01`, `esp32_02`, and
`esp32_greenhouse_north`. The dashboard and `/api/sensor?device_id=...` filter
data by this value.
