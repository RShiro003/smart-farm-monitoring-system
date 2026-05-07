#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <time.h>

const char* ssid = "TP-Link_31CA";
const char* password = "54299979";

const char* serverUrl = "http://192.168.1.106:5000/api/sensor";
// Set a unique DEVICE_ID for each ESP32 board before flashing.
const char* DEVICE_ID = "esp32_dummy";

// 한국 시간 설정
const long gmtOffset_sec = 9 * 3600;
const int daylightOffset_sec = 0;

String getTimeString() {
  struct tm timeinfo;

  if (!getLocalTime(&timeinfo)) {
    return "time_sync_failed";
  }

  char timeString[25];
  strftime(timeString, sizeof(timeString), "%Y-%m-%d %H:%M:%S", &timeinfo);

  return String(timeString);
}

void setup() {
  Serial.begin(115200);

  WiFi.begin(ssid, password);
  Serial.print("WiFi connecting");

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.println("WiFi connected");
  Serial.print("ESP32 IP: ");
  Serial.println(WiFi.localIP());

  // NTP 서버로 현재 시간 동기화
  configTime(gmtOffset_sec, daylightOffset_sec, "pool.ntp.org", "time.nist.gov");

  Serial.print("Time syncing");
  while (getTimeString() == "time_sync_failed") {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.print("Current time: ");
  Serial.println(getTimeString());
}

void loop() {
  float temperature = random(200, 350) / 10.0;  // 20.0 ~ 35.0
  float humidity = random(400, 800) / 10.0;     // 40.0 ~ 80.0
  int soilMoisture = random(0, 101);            // 0 ~ 100
  int light = random(0, 101);                   // 0 ~ 100

  String timestamp = getTimeString();

  Serial.print("Timestamp: ");
  Serial.println(timestamp);

  Serial.print("Temperature: ");
  Serial.println(temperature);

  Serial.print("Humidity: ");
  Serial.println(humidity);

  Serial.print("Soil moisture: ");
  Serial.println(soilMoisture);

  Serial.print("Light: ");
  Serial.println(light);

  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;

    http.begin(serverUrl);
    http.addHeader("Content-Type", "application/json");

    String jsonData = "{";
    jsonData += "\"device_id\":\"";
    jsonData += DEVICE_ID;
    jsonData += "\",";
    jsonData += "\"timestamp\":\"" + timestamp + "\",";
    jsonData += "\"temperature\":" + String(temperature, 2) + ",";
    jsonData += "\"humidity\":" + String(humidity, 2) + ",";
    jsonData += "\"soil_moisture\":" + String(soilMoisture) + ",";
    jsonData += "\"light\":" + String(light);
    jsonData += "}";

    int responseCode = http.POST(jsonData);

    Serial.print("Send: ");
    Serial.println(jsonData);

    Serial.print("Response code: ");
    Serial.println(responseCode);

    String response = http.getString();
    Serial.println(response);

    http.end();
  } else {
    Serial.println("WiFi disconnected");
  }

  delay(5000);
}
