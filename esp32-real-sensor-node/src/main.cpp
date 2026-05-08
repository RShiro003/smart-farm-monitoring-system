#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <time.h>

const char* ssid = "TP-Link_31CA";
const char* password = "54299979";

const char* serverUrl = "http://192.168.1.106:5000/api/sensor";
const char* thresholdsUrl = "http://192.168.1.106:5000/api/thresholds";
// Set a unique DEVICE_ID for each ESP32 board before flashing.
const char* DEVICE_ID = "esp32_sensor";
const unsigned long THRESHOLD_FETCH_INTERVAL_MS = 45000;
const unsigned long HTTP_TIMEOUT_MS = 3000;
const unsigned long WIFI_CONNECT_TIMEOUT_MS = 15000;

// 한국 시간 UTC+9
const long gmtOffset_sec = 9 * 3600;
const int daylightOffset_sec = 0;

// 센서 핀 설정
#define DHT_PIN 27
#define DHT_TYPE DHT22

#define LIGHT_DO_PIN 33
#define SOIL_AO_PIN 35
#define SOIL_DO_PIN 26

#define LED_GREEN_PIN 16
#define LED_RED_PIN 17
#define LED_WHITE_PIN 18

DHT dht(DHT_PIN, DHT_TYPE);

struct ThresholdSettings {
  float temperatureMin;
  float temperatureMax;
  float humidityMin;
  float humidityMax;
  float soilMoistureMin;
  float soilMoistureMax;
  float lightMin;
  float lightMax;
};

enum FarmStatus {
  STATUS_NORMAL,
  STATUS_ABNORMAL,
  STATUS_SOIL_DRY
};

ThresholdSettings thresholds = {
  18.0, 25.0,
  60.0, 80.0,
  40.0, 70.0,
  0.0, 100.0
};

unsigned long lastThresholdFetchAt = 0;
bool thresholdFetchAttempted = false;

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

  unsigned long startedAt = millis();
  while (WiFi.status() != WL_CONNECTED &&
         millis() - startedAt < WIFI_CONNECT_TIMEOUT_MS) {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("WiFi connected");
    Serial.print("ESP32 IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("WiFi connect timeout. Will retry later.");
  }
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

String farmStatusName(FarmStatus status) {
  if (status == STATUS_SOIL_DRY) {
    return "SOIL_DRY";
  }
  if (status == STATUS_ABNORMAL) {
    return "ABNORMAL";
  }
  return "NORMAL";
}

void printThresholds() {
  Serial.println("Applied thresholds:");
  Serial.print("  temperature: ");
  Serial.print(thresholds.temperatureMin);
  Serial.print(" ~ ");
  Serial.println(thresholds.temperatureMax);

  Serial.print("  humidity: ");
  Serial.print(thresholds.humidityMin);
  Serial.print(" ~ ");
  Serial.println(thresholds.humidityMax);

  Serial.print("  soil_moisture: ");
  Serial.print(thresholds.soilMoistureMin);
  Serial.print(" ~ ");
  Serial.println(thresholds.soilMoistureMax);

  Serial.print("  light: ");
  Serial.print(thresholds.lightMin);
  Serial.print(" ~ ");
  Serial.println(thresholds.lightMax);
}

bool fetchThresholdsFromServer() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Threshold GET skipped: WiFi disconnected");
    return false;
  }

  HTTPClient http;
  String requestUrl = String(thresholdsUrl) + "?device_id=" + String(DEVICE_ID);

  http.begin(requestUrl);
  http.setTimeout(HTTP_TIMEOUT_MS);

  int responseCode = http.GET();
  if (responseCode != HTTP_CODE_OK) {
    Serial.print("Threshold GET failed. Response code: ");
    Serial.println(responseCode);
    http.end();
    return false;
  }

  String payload = http.getString();
  StaticJsonDocument<512> doc;
  DeserializationError error = deserializeJson(doc, payload);
  if (error) {
    Serial.print("Threshold JSON parse failed: ");
    Serial.println(error.c_str());
    http.end();
    return false;
  }

  ThresholdSettings next = thresholds;
  next.temperatureMin = doc["temperature_min"] | thresholds.temperatureMin;
  next.temperatureMax = doc["temperature_max"] | thresholds.temperatureMax;
  next.humidityMin = doc["humidity_min"] | thresholds.humidityMin;
  next.humidityMax = doc["humidity_max"] | thresholds.humidityMax;
  next.soilMoistureMin = doc["soil_moisture_min"] | thresholds.soilMoistureMin;
  next.soilMoistureMax = doc["soil_moisture_max"] | thresholds.soilMoistureMax;
  next.lightMin = doc["light_min"] | thresholds.lightMin;
  next.lightMax = doc["light_max"] | thresholds.lightMax;

  thresholds = next;
  Serial.println("Threshold GET success");
  printThresholds();

  http.end();
  return true;
}

FarmStatus evaluateFarmStatus(float temperature, float humidity, int soilMoisture, int light) {
  if (soilMoisture < thresholds.soilMoistureMin) {
    return STATUS_SOIL_DRY;
  }

  if (temperature < thresholds.temperatureMin || temperature > thresholds.temperatureMax) {
    return STATUS_ABNORMAL;
  }

  if (humidity < thresholds.humidityMin || humidity > thresholds.humidityMax) {
    return STATUS_ABNORMAL;
  }

  if (light < thresholds.lightMin || light > thresholds.lightMax) {
    return STATUS_ABNORMAL;
  }

  return STATUS_NORMAL;
}

void turnOffAllLEDs() {
  digitalWrite(LED_GREEN_PIN, LOW);
  digitalWrite(LED_RED_PIN, LOW);
  digitalWrite(LED_WHITE_PIN, LOW);
}

void updateStatusLED(FarmStatus status) {
  turnOffAllLEDs();

  if (status == STATUS_SOIL_DRY) {
    digitalWrite(LED_WHITE_PIN, HIGH);
  } else if (status == STATUS_ABNORMAL) {
    digitalWrite(LED_RED_PIN, HIGH);
  } else {
    digitalWrite(LED_GREEN_PIN, HIGH);
  }
}

void setup() {
  delay(2000);

  Serial.begin(115200);

  dht.begin();

  pinMode(LIGHT_DO_PIN, INPUT);
  pinMode(SOIL_DO_PIN, INPUT);
  pinMode(LED_GREEN_PIN, OUTPUT);
  pinMode(LED_RED_PIN, OUTPUT);
  pinMode(LED_WHITE_PIN, OUTPUT);
  turnOffAllLEDs();

  connectWiFi();
  if (WiFi.status() == WL_CONNECTED) {
    syncTime();
  }
  printThresholds();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi disconnected. Reconnecting...");
    connectWiFi();
    if (WiFi.status() == WL_CONNECTED) {
      syncTime();
    }
  }

  unsigned long now = millis();
  if (WiFi.status() == WL_CONNECTED &&
      (!thresholdFetchAttempted ||
       now - lastThresholdFetchAt >= THRESHOLD_FETCH_INTERVAL_MS)) {
    lastThresholdFetchAt = now;
    thresholdFetchAttempted = true;
    fetchThresholdsFromServer();
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

  printThresholds();

  FarmStatus status = evaluateFarmStatus(temperature, humidity, soilMoisture, lightDigital);
  updateStatusLED(status);
  Serial.print("LED state: ");
  Serial.println(farmStatusName(status));

  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;

    http.begin(serverUrl);
    http.setTimeout(HTTP_TIMEOUT_MS);
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
