#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <DHT.h>
#include <time.h>

const char* ssid = "TP-Link_31CA";
const char* password = "54299979";

const char* serverUrl = "http://192.168.1.106:5000/api/sensor";
// Set a unique DEVICE_ID for each ESP32 board before flashing.
const char* DEVICE_ID = "esp32_sensor";

// 한국 시간 UTC+9
const long gmtOffset_sec = 9 * 3600;
const int daylightOffset_sec = 0;

// 센서 핀 설정
#define DHT_PIN 27
#define DHT_TYPE DHT22

#define LIGHT_DO_PIN 33
#define SOIL_AO_PIN 35
#define SOIL_DO_PIN 26

DHT dht(DHT_PIN, DHT_TYPE);

String getTimestamp() {
  struct tm timeinfo;

  if (!getLocalTime(&timeinfo)) {
    return "time_not_set";
  }

  char buffer[25];
  strftime(buffer, sizeof(buffer), "%Y-%m-%d %H:%M:%S", &timeinfo);

  return String(buffer);
}

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);

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
}

void syncTime() {
  configTime(gmtOffset_sec, daylightOffset_sec, "pool.ntp.org", "time.nist.gov");

  Serial.print("Time syncing");

  struct tm timeinfo;
  int retry = 0;

  while (!getLocalTime(&timeinfo) && retry < 20) {
    delay(500);
    Serial.print(".");
    retry++;
  }

  Serial.println();

  if (retry >= 20) {
    Serial.println("Time sync failed");
  } else {
    Serial.println("Time synced");
    Serial.print("Current time: ");
    Serial.println(getTimestamp());
  }
}

void setup() {
  delay(2000);

  Serial.begin(115200);

  dht.begin();

  pinMode(LIGHT_DO_PIN, INPUT);
  pinMode(SOIL_DO_PIN, INPUT);

  connectWiFi();
  syncTime();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi disconnected. Reconnecting...");
    connectWiFi();
    syncTime();
  }

  String timestamp = getTimestamp();

  float temperature = dht.readTemperature();
  float humidity = dht.readHumidity();

  int soilRaw = analogRead(SOIL_AO_PIN);
  int soilDigital = digitalRead(SOIL_DO_PIN);
  int lightDigital = digitalRead(LIGHT_DO_PIN);

  if (isnan(temperature) || isnan(humidity)) {
    Serial.println("Failed to read from DHT sensor");
    temperature = -1;
    humidity = -1;
  }

  // 토양수분 변환
  // 일반적으로 soilRaw 값이 클수록 건조, 작을수록 습함
  int soilMoisture = map(soilRaw, 4095, 0, 0, 100);
  soilMoisture = constrain(soilMoisture, 0, 100);

  Serial.println("----- SENSOR DATA -----");

  Serial.print("Timestamp: ");
  Serial.println(timestamp);

  Serial.print("Temperature: ");
  Serial.println(temperature);

  Serial.print("Humidity: ");
  Serial.println(humidity);

  Serial.print("Soil raw: ");
  Serial.print(soilRaw);
  Serial.print(" / Soil moisture: ");
  Serial.println(soilMoisture);

  Serial.print("Soil DO: ");
  Serial.println(soilDigital);

  Serial.print("Light DO: ");
  Serial.println(lightDigital);

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
    jsonData += "\"soil_raw\":" + String(soilRaw) + ",";
    jsonData += "\"soil_digital\":" + String(soilDigital) + ",";
    jsonData += "\"light\":" + String(lightDigital) + ",";
    jsonData += "\"light_digital\":" + String(lightDigital);
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
    Serial.println("WiFi disconnected. Data not sent.");
  }

  delay(5000);
}
