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

const unsigned long ANOMALY_MIN_INTERVAL_MS = 120000;
const unsigned long ANOMALY_MAX_INTERVAL_MS = 300000;

float currentTemperature = 24.0;
float currentHumidity = 68.0;
float currentSoilMoisture = 66.0;
int currentLight = 70;

enum DummyAnomalyState {
  ANOMALY_NONE,
  ANOMALY_TEMP_HIGH,
  ANOMALY_HUMIDITY_LOW,
  ANOMALY_SOIL_DRY
};

DummyAnomalyState currentAnomaly = ANOMALY_NONE;
int anomalyLoopsRemaining = 0;
unsigned long nextAnomalyAt = 0;
float anomalyTargetTemperature = 30.0;
float anomalyTargetHumidity = 50.0;
float anomalyTargetSoilMoisture = 33.0;

bool wateringActive = false;
int wateringLoopsRemaining = 0;
float wateringTargetSoilMoisture = 68.0;

const char* dummySensorStatus = "NORMAL";
int currentNtpHour = -1;

float clampFloat(float value, float minValue, float maxValue) {
  if (value < minValue) {
    return minValue;
  }
  if (value > maxValue) {
    return maxValue;
  }
  return value;
}

int clampInt(int value, int minValue, int maxValue) {
  if (value < minValue) {
    return minValue;
  }
  if (value > maxValue) {
    return maxValue;
  }
  return value;
}

float randomFloat(float minValue, float maxValue) {
  return minValue + (maxValue - minValue) * (float)random(0, 10001) / 10000.0;
}

int getCurrentHourFromNTP() {
  struct tm timeinfo;

  if (!getLocalTime(&timeinfo)) {
    return -1;
  }

  return timeinfo.tm_hour;
}

int getCurrentMinuteOfDayFromNTP() {
  struct tm timeinfo;

  if (!getLocalTime(&timeinfo)) {
    return -1;
  }

  return timeinfo.tm_hour * 60 + timeinfo.tm_min;
}

String getTimeString() {
  struct tm timeinfo;

  if (!getLocalTime(&timeinfo)) {
    return "time_sync_failed";
  }

  char timeString[25];
  strftime(timeString, sizeof(timeString), "%Y-%m-%d %H:%M:%S", &timeinfo);

  return String(timeString);
}

const char* anomalyStateName(DummyAnomalyState state) {
  if (state == ANOMALY_TEMP_HIGH) {
    return "TEMP_HIGH";
  }
  if (state == ANOMALY_HUMIDITY_LOW) {
    return "HUMIDITY_LOW";
  }
  if (state == ANOMALY_SOIL_DRY) {
    return "SOIL_DRY";
  }
  return "NORMAL";
}

void scheduleNextAnomaly() {
  unsigned long interval = (unsigned long)random(
    (long)ANOMALY_MIN_INTERVAL_MS,
    (long)ANOMALY_MAX_INTERVAL_MS + 1
  );
  nextAnomalyAt = millis() + interval;
}

void startAnomalyIfNeeded() {
  if (currentAnomaly != ANOMALY_NONE || nextAnomalyAt == 0 || millis() < nextAnomalyAt) {
    return;
  }

  currentAnomaly = (DummyAnomalyState)random(1, 4);

  if (currentAnomaly == ANOMALY_TEMP_HIGH) {
    anomalyLoopsRemaining = random(10, 17);
    anomalyTargetTemperature = randomFloat(29.0, 31.0);
  } else if (currentAnomaly == ANOMALY_HUMIDITY_LOW) {
    anomalyLoopsRemaining = random(10, 17);
    anomalyTargetHumidity = randomFloat(49.0, 52.0);
  } else {
    anomalyLoopsRemaining = random(12, 19);
    anomalyTargetSoilMoisture = randomFloat(31.0, 35.0);
    wateringActive = false;
  }
}

float getTemperatureTargetByHour(int hour) {
  if (hour < 0) {
    return 24.0;
  }
  if (hour >= 10 && hour < 17) {
    return 25.8;
  }
  if (hour >= 6 && hour < 10) {
    return 22.8 + (hour - 6) * 0.7;
  }
  if (hour >= 17 && hour < 22) {
    return 25.2 - (hour - 17) * 0.55;
  }
  return 22.2;
}

float getHumidityTarget(float temperature, int hour) {
  float target = 70.0 - (temperature - 24.0) * 2.2;

  if (hour >= 10 && hour < 17) {
    target -= 2.0;
  } else if (hour >= 22 || (hour >= 0 && hour < 6)) {
    target += 3.0;
  }

  return clampFloat(target, 55.0, 80.0);
}

int getLightTargetByMinute(int minuteOfDay) {
  if (minuteOfDay < 0) {
    return 70;
  }

  if (minuteOfDay < 360) {
    return random(3, 16);
  }
  if (minuteOfDay < 480) {
    return 10 + (minuteOfDay - 360) * 55 / 120;
  }
  if (minuteOfDay < 1080) {
    return random(75, 96);
  }
  if (minuteOfDay < 1260) {
    return random(65, 86);
  }
  if (minuteOfDay < 1380) {
    return 40 - (minuteOfDay - 1260) * 30 / 120;
  }

  return random(3, 16);
}

void generateDummySensorData() {
  if (nextAnomalyAt == 0) {
    scheduleNextAnomaly();
  }

  startAnomalyIfNeeded();

  DummyAnomalyState activeAnomaly = currentAnomaly;
  dummySensorStatus = anomalyStateName(activeAnomaly);

  currentNtpHour = getCurrentHourFromNTP();
  int minuteOfDay = getCurrentMinuteOfDayFromNTP();

  float temperatureTarget = getTemperatureTargetByHour(currentNtpHour);
  float temperatureMin = 20.0;
  float temperatureMax = 28.0;
  float temperatureMaxStep = 0.45;

  if (activeAnomaly == ANOMALY_TEMP_HIGH) {
    temperatureTarget = anomalyTargetTemperature;
    temperatureMax = 31.0;
    temperatureMaxStep = 0.70;
  }

  float temperatureStep = (temperatureTarget - currentTemperature) * 0.15;
  temperatureStep += randomFloat(-0.30, 0.30);
  temperatureStep = clampFloat(temperatureStep, -temperatureMaxStep, temperatureMaxStep);
  currentTemperature = clampFloat(currentTemperature + temperatureStep, temperatureMin, temperatureMax);

  float humidityTarget = getHumidityTarget(currentTemperature, currentNtpHour);
  float humidityMin = 55.0;
  float humidityMaxStep = 1.5;

  if (activeAnomaly == ANOMALY_HUMIDITY_LOW) {
    humidityTarget = anomalyTargetHumidity;
    humidityMin = 48.0;
  }

  float humidityStep = (humidityTarget - currentHumidity) * 0.12;
  humidityStep += randomFloat(-1.0, 1.0);
  humidityStep = clampFloat(humidityStep, -humidityMaxStep, humidityMaxStep);
  currentHumidity = clampFloat(currentHumidity + humidityStep, humidityMin, 80.0);

  if (activeAnomaly == ANOMALY_SOIL_DRY) {
    float soilStep = (anomalyTargetSoilMoisture - currentSoilMoisture) * 0.20;
    soilStep += randomFloat(-0.35, 0.10);
    soilStep = clampFloat(soilStep, -2.8, 0.2);
    currentSoilMoisture = clampFloat(currentSoilMoisture + soilStep, 30.0, 75.0);
  } else {
    if (!wateringActive && currentSoilMoisture <= 38.0) {
      wateringActive = true;
      wateringLoopsRemaining = random(4, 8);
      wateringTargetSoilMoisture = randomFloat(60.0, 75.0);
    }

    if (wateringActive) {
      currentSoilMoisture += randomFloat(3.0, 7.0);
      wateringLoopsRemaining--;

      if (currentSoilMoisture >= wateringTargetSoilMoisture || wateringLoopsRemaining <= 0) {
        wateringActive = false;
      }
    } else {
      currentSoilMoisture -= randomFloat(0.0, 1.0);
      currentSoilMoisture += randomFloat(-0.10, 0.10);
    }

    currentSoilMoisture = clampFloat(currentSoilMoisture, 35.0, 75.0);
  }

  int lightTarget = getLightTargetByMinute(minuteOfDay);
  int lightStep = clampInt(lightTarget - currentLight, -5, 5);
  currentLight += lightStep + random(-2, 3);
  currentLight = clampInt(currentLight, 0, 100);

  if (minuteOfDay >= 0 && (minuteOfDay < 360 || minuteOfDay >= 1380)) {
    currentLight = clampInt(currentLight, 0, 20);
  }

  if (activeAnomaly != ANOMALY_NONE) {
    anomalyLoopsRemaining--;

    if (anomalyLoopsRemaining <= 0) {
      currentAnomaly = ANOMALY_NONE;
      scheduleNextAnomaly();
    }
  }
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

  randomSeed((uint32_t)micros());
  scheduleNextAnomaly();
}

void loop() {
  generateDummySensorData();

  float temperature = currentTemperature;
  float humidity = currentHumidity;
  int soilMoisture = (int)(currentSoilMoisture + 0.5);
  int light = currentLight;

  String timestamp = getTimeString();

  Serial.println("----- DUMMY SENSOR DATA -----");

  Serial.print("Timestamp: ");
  Serial.println(timestamp);

  Serial.print("Dummy state: ");
  Serial.println(dummySensorStatus);

  Serial.print("NTP hour: ");
  Serial.println(currentNtpHour);

  Serial.print("Temperature: ");
  Serial.println(temperature);

  Serial.print("Humidity: ");
  Serial.println(humidity);

  Serial.print("Soil moisture: ");
  Serial.println(soilMoisture);

  Serial.print("Light: ");
  Serial.println(light);

  Serial.print("Watering: ");
  Serial.println(wateringActive ? "ON" : "OFF");

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
