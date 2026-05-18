#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <time.h>

// 더미 노드는 실제 센서가 없어도 Flask 서버와 대시보드 흐름을 테스트하기 위한 ESP32 코드다.
// Wi-Fi 연결, NTP 시간 동기화, JSON 생성, /api/sensor POST 전송은 실제 노드와 같은 흐름을 사용하고,
// 센서값만 시간대/랜덤/이상상태 시뮬레이션으로 만든다.
const char* ssid = "TP-Link_31CA";
const char* password = "54299979";

const char* serverUrl = "http://192.168.1.106:5000/api/sensor";
// 여러 ESP32가 같은 서버로 데이터를 보내므로, 플래시 전에 보드마다 다른 DEVICE_ID를 넣어야 한다.
// 서버와 대시보드는 이 값을 기준으로 데이터 필터링과 장치 선택을 수행한다.
const char* DEVICE_ID = "esp32_dummy";

// ESP32 timestamp는 NTP로 맞춘 한국 시간(UTC+9)을 사용한다.
// 서버는 별도로 server_received_at을 저장하므로, ESP32 시간이 실패해도 수신 시각은 남는다.
const long gmtOffset_sec = 9 * 3600;
const int daylightOffset_sec = 0;

// 이상상태는 너무 자주 발생하면 실제 환경처럼 보이지 않으므로,
// 다음 이상상태 발생 시점을 2~5분 사이에서 랜덤하게 잡는다.
const unsigned long ANOMALY_MIN_INTERVAL_MS = 120000;
const unsigned long ANOMALY_MAX_INTERVAL_MS = 300000;

// 현재 더미 센서값은 loop마다 목표값을 향해 조금씩 이동한다.
// 완전 랜덤값을 매번 보내면 그래프가 튀기 때문에, 실제 센서처럼 완만한 추세를 만들기 위한 상태값이다.
float currentTemperature = 24.0;
float currentHumidity = 68.0;
float currentSoilMoisture = 66.0;
int currentLight = 70;

// 더미 데이터가 정상값만 반복되면 임계값/대시보드 상태 표시를 검증하기 어렵다.
// 아래 상태들은 고온, 저습도, 토양 건조 상황을 일정 시간 동안 만들어 주는 시나리오다.
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

// 토양 건조 이상상태가 끝난 뒤 물을 준 것처럼 수분이 회복되는 구간을 시뮬레이션한다.
bool wateringActive = false;
int wateringLoopsRemaining = 0;
float wateringTargetSoilMoisture = 68.0;

const char* dummySensorStatus = "NORMAL";
int currentNtpHour = -1;

float clampFloat(float value, float minValue, float maxValue) {
  // 더미값이 현실적인 센서 범위를 벗어나지 않도록 상한/하한을 고정한다.
  if (value < minValue) {
    return minValue;
  }
  if (value > maxValue) {
    return maxValue;
  }
  return value;
}

int clampInt(int value, int minValue, int maxValue) {
  // 조도처럼 정수로 다루는 값도 같은 방식으로 범위를 제한한다.
  if (value < minValue) {
    return minValue;
  }
  if (value > maxValue) {
    return maxValue;
  }
  return value;
}

float randomFloat(float minValue, float maxValue) {
  // Arduino random()은 정수 기반이므로, 센서값 흔들림을 만들기 위해 float 범위로 변환한다.
  return minValue + (maxValue - minValue) * (float)random(0, 10001) / 10000.0;
}

int getCurrentHourFromNTP() {
  // 시간대별 온도/조도 패턴을 만들기 위해 현재 시(hour)를 사용한다.
  // NTP가 아직 준비되지 않았으면 -1을 반환해 기본 목표값을 쓰게 한다.
  struct tm timeinfo;

  if (!getLocalTime(&timeinfo)) {
    return -1;
  }

  return timeinfo.tm_hour;
}

int getCurrentMinuteOfDayFromNTP() {
  // 조도는 일출/주간/일몰 패턴이 중요하므로 하루 중 몇 번째 분인지로 목표값을 계산한다.
  struct tm timeinfo;

  if (!getLocalTime(&timeinfo)) {
    return -1;
  }

  return timeinfo.tm_hour * 60 + timeinfo.tm_min;
}

String getTimeString() {
  // ESP32가 센서를 읽은 시각을 timestamp로 서버에 보낸다.
  // 서버의 server_received_at과 비교하면 네트워크 지연이나 NTP 실패 여부를 구분할 수 있다.
  struct tm timeinfo;

  if (!getLocalTime(&timeinfo)) {
    return "time_sync_failed";
  }

  char timeString[25];
  strftime(timeString, sizeof(timeString), "%Y-%m-%d %H:%M:%S", &timeinfo);

  return String(timeString);
}

const char* anomalyStateName(DummyAnomalyState state) {
  // Serial 로그에서 현재 더미 시나리오를 사람이 읽기 쉽게 표시하기 위한 이름 변환이다.
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
  // 현재 이상상태가 끝난 뒤 다음 이상상태 발생 시각을 예약한다.
  // millis() 기준이라 loop가 계속 돌아도 별도 timer 없이 조건을 확인할 수 있다.
  unsigned long interval = (unsigned long)random(
    (long)ANOMALY_MIN_INTERVAL_MS,
    (long)ANOMALY_MAX_INTERVAL_MS + 1
  );
  nextAnomalyAt = millis() + interval;
}

void startAnomalyIfNeeded() {
  // 예약 시간이 되었고 아직 이상상태가 없을 때 하나의 시나리오를 시작한다.
  // 한 번에 여러 이상상태가 겹치지 않게 currentAnomaly가 NORMAL일 때만 시작한다.
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
  // 실제 온실은 낮에 온도가 올라가고 새벽/밤에는 내려간다.
  // 더미 노드도 시간대별 목표 온도를 두어 대시보드 그래프가 자연스러운 일변화를 보이게 한다.
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
  // 온도가 높아지면 상대습도가 낮아지는 경향을 반영한다.
  // 시간대 보정까지 더해 더미 습도가 온도와 독립적으로 완전 랜덤하게 움직이지 않게 한다.
  float target = 70.0 - (temperature - 24.0) * 2.2;

  if (hour >= 10 && hour < 17) {
    target -= 2.0;
  } else if (hour >= 22 || (hour >= 0 && hour < 6)) {
    target += 3.0;
  }

  return clampFloat(target, 55.0, 80.0);
}

int getLightTargetByMinute(int minuteOfDay) {
  // 조도는 하루 중 시간에 따라 가장 크게 달라진다.
  // 새벽/밤은 낮게, 낮 시간은 높게 만들어 차트에서 낮밤 패턴을 확인할 수 있게 한다.
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
  // loop마다 호출되어 다음 전송에 사용할 더미 센서값을 갱신한다.
  // 정상 패턴, 이상상태, 물주기 회복, 조도 일변화를 모두 이 함수에서 반영한다.
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
    // 고온 이상상태에서는 목표 온도와 허용 상한을 올려 임계값 초과 상황을 만든다.
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
    // 저습도 이상상태에서는 습도 목표를 낮춰 대시보드 경고 표시를 확인할 수 있게 한다.
    humidityTarget = anomalyTargetHumidity;
    humidityMin = 48.0;
  }

  float humidityStep = (humidityTarget - currentHumidity) * 0.12;
  humidityStep += randomFloat(-1.0, 1.0);
  humidityStep = clampFloat(humidityStep, -humidityMaxStep, humidityMaxStep);
  currentHumidity = clampFloat(currentHumidity + humidityStep, humidityMin, 80.0);

  if (activeAnomaly == ANOMALY_SOIL_DRY) {
    // 토양 건조 이상상태는 수분이 빠르게 떨어지는 방향으로 값을 이동시킨다.
    float soilStep = (anomalyTargetSoilMoisture - currentSoilMoisture) * 0.20;
    soilStep += randomFloat(-0.35, 0.10);
    soilStep = clampFloat(soilStep, -2.8, 0.2);
    currentSoilMoisture = clampFloat(currentSoilMoisture + soilStep, 30.0, 75.0);
  } else {
    if (!wateringActive && currentSoilMoisture <= 38.0) {
      // 건조 상태가 충분히 심해지면 물을 준 상황을 시뮬레이션한다.
      // 이후 몇 loop 동안 토양수분이 빠르게 회복되어 그래프에 관수 패턴이 나타난다.
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
    // 이상상태는 정해진 loop 수만 유지하고, 끝나면 다시 정상 패턴으로 돌아간다.
    anomalyLoopsRemaining--;

    if (anomalyLoopsRemaining <= 0) {
      currentAnomaly = ANOMALY_NONE;
      scheduleNextAnomaly();
    }
  }
}

void setup() {
  Serial.begin(115200);

  // 서버로 POST하려면 Wi-Fi 연결이 먼저 필요하다.
  // 더미 노드는 테스트용이라 연결될 때까지 대기한 뒤 다음 단계로 진행한다.
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

  // NTP 서버로 현재 시간 동기화.
  // 이 시간이 JSON의 timestamp로 들어가고, 서버는 별도로 수신 시각을 저장한다.
  configTime(gmtOffset_sec, daylightOffset_sec, "pool.ntp.org", "time.nist.gov");

  Serial.print("Time syncing");
  while (getTimeString() == "time_sync_failed") {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.print("Current time: ");
  Serial.println(getTimeString());

  // 랜덤 패턴이 매번 같지 않도록 시드 초기화 후 첫 이상상태 예약을 만든다.
  randomSeed((uint32_t)micros());
  scheduleNextAnomaly();
}

void loop() {
  // 1) 더미 센서값 생성
  // 2) 현재 timestamp 생성
  // 3) JSON payload 조립
  // 4) Flask 서버의 /api/sensor로 POST
  // 이 순서를 5초마다 반복해 실제 센서 노드와 같은 서버 저장 흐름을 검증한다.
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

    // Flask 서버의 센서 수집 API로 전송한다.
    // Content-Type을 JSON으로 지정해야 request.get_json()이 정상적으로 body를 파싱한다.
    http.begin(serverUrl);
    http.addHeader("Content-Type", "application/json");

    String jsonData = "{";
    // 서버는 device_id, temperature, humidity, soil_moisture, light를 필수로 검증한다.
    // timestamp는 ESP32 측정 시각이며, 서버는 수신 시각을 server_received_at으로 별도 저장한다.
    jsonData += "\"device_id\":\"";
    jsonData += DEVICE_ID;
    jsonData += "\",";
    jsonData += "\"timestamp\":\"" + timestamp + "\",";
    jsonData += "\"temperature\":" + String(temperature, 2) + ",";
    jsonData += "\"humidity\":" + String(humidity, 2) + ",";
    jsonData += "\"soil_moisture\":" + String(soilMoisture) + ",";
    jsonData += "\"light\":" + String(light);
    jsonData += "}";

    // HTTP 응답 코드는 서버 저장 성공 여부를 확인하는 1차 신호다.
    // 정상 저장이면 Flask는 201과 함께 저장된 data JSON을 반환한다.
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
