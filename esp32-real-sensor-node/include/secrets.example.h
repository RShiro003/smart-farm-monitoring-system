#pragma once

const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* SERVER_URL = "http://YOUR_RASPBERRY_PI_IP:5000/api/sensor";
const char* THRESHOLDS_URL = "http://YOUR_RASPBERRY_PI_IP:5000/api/thresholds";
const char* DEVICE_ID = "esp32_sensor";

// 서버에 SMART_FARM_API_KEY를 설정했다면 같은 값을 여기에 넣는다.
// 서버가 키를 쓰지 않으면 빈 문자열로 두면 되고, 그때는 헤더를 보내지 않는다.
const char* API_KEY = "";
