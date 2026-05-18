#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <time.h>

// 실제 센서 노드용 ESP32 코드다.
// DHT22, 토양수분 센서, 조도 디지털 센서를 읽고 Flask 서버의 /api/sensor로 JSON을 전송한다.
// 또한 서버의 /api/thresholds에서 장치별 임계값을 가져와 LED 상태 표시 기준으로 사용한다.
const char* ssid = "TP-Link_31CA";
const char* password = "54299979";

const char* serverUrl = "http://192.168.1.106:5000/api/sensor";
const char* thresholdsUrl = "http://192.168.1.106:5000/api/thresholds";
// 여러 ESP32가 한 Raspberry Pi 서버로 데이터를 보내므로 보드마다 고유한 DEVICE_ID가 필요하다.
// 서버 DB의 device_id 컬럼, 대시보드 장치 필터, 임계값 설정 조회가 모두 이 값으로 연결된다.
const char* DEVICE_ID = "esp32_sensor";
// 임계값은 사용자가 대시보드에서 바꿀 수 있으므로 주기적으로 서버에서 다시 가져온다.
const unsigned long THRESHOLD_FETCH_INTERVAL_MS = 45000;
// 네트워크가 불안정할 때 loop가 오래 멈추지 않도록 HTTP/Wi-Fi 대기 시간을 제한한다.
const unsigned long HTTP_TIMEOUT_MS = 3000;
const unsigned long WIFI_CONNECT_TIMEOUT_MS = 15000;

// ESP32 timestamp는 NTP로 맞춘 한국 시간(UTC+9)을 사용한다.
// 서버는 server_received_at을 별도로 저장하므로 ESP32 시간과 서버 수신 시간을 구분할 수 있다.
const long gmtOffset_sec = 9 * 3600;
const int daylightOffset_sec = 0;

// 센서 핀 설정.
// DHT22는 온도/습도, 토양 센서는 아날로그 원시값과 디지털 상태, 조도 센서는 디지털 상태를 읽는다.
#define DHT_PIN 27
#define DHT_TYPE DHT22

#define LIGHT_DO_PIN 33
#define SOIL_AO_PIN 35
#define SOIL_DO_PIN 26

#define LED_GREEN_PIN 16
#define LED_RED_PIN 17
#define LED_WHITE_PIN 18

DHT dht(DHT_PIN, DHT_TYPE);

// 서버에서 내려받는 장치별 임계값 구조체다.
// evaluateFarmStatus()가 현재 측정값과 이 값을 비교해 LED 상태를 결정한다.
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

// LED로 표현할 농장 상태.
// 토양 건조는 다른 이상보다 우선해서 흰색 LED로 표시한다.
enum FarmStatus {
  STATUS_NORMAL,
  STATUS_ABNORMAL,
  STATUS_SOIL_DRY
};

// 서버에서 임계값을 아직 가져오지 못했을 때 사용하는 기본 기준값이다.
// setup 직후에도 LED 판단이 가능해야 하므로 로컬 기본값을 먼저 가지고 시작한다.
ThresholdSettings thresholds = {
  18.0, 25.0,
  60.0, 80.0,
  40.0, 70.0,
  0.0, 100.0
};

unsigned long lastThresholdFetchAt = 0;
bool thresholdFetchAttempted = false;

String getTimestamp() {
  // 센서값을 읽은 ESP32 기준 시간을 JSON timestamp로 보낸다.
  // NTP 동기화가 실패하면 서버가 문자열을 그대로 저장하고, 대시보드는 server_received_at을 우선 사용한다.
  struct tm timeinfo;

  if (!getLocalTime(&timeinfo)) {
    return "time_not_set";
  }

  char buffer[25];
  strftime(buffer, sizeof(buffer), "%Y-%m-%d %H:%M:%S", &timeinfo);

  return String(buffer);
}

void connectWiFi() {
  // 서버와 통신하려면 Wi-Fi 연결이 필요하다.
  // 무한 대기하지 않고 제한 시간 후 빠져나와 loop에서 재시도하게 만든다.
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
  // NTP 동기화는 timestamp 품질을 높이기 위한 단계다.
  // 실패해도 센서 전송 자체는 계속 가능하며, 서버 수신 시각이 별도로 저장된다.
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
  // LED 상태를 Serial 로그에 사람이 읽을 수 있는 문자열로 남기기 위한 변환 함수다.
  if (status == STATUS_SOIL_DRY) {
    return "SOIL_DRY";
  }
  if (status == STATUS_ABNORMAL) {
    return "ABNORMAL";
  }
  return "NORMAL";
}

void printThresholds() {
  // 현재 ESP32가 적용 중인 임계값을 Serial Monitor에 출력한다.
  // 대시보드에서 저장한 값이 실제 노드에 반영됐는지 현장에서 확인할 때 필요하다.
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
  // 서버의 /api/thresholds?device_id=...를 호출해 이 장치에 적용할 임계값을 가져온다.
  // Wi-Fi가 끊겨 있거나 서버 응답/JSON 파싱이 실패하면 기존 thresholds 값을 유지한다.
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
  // 각 필드가 응답에 없으면 기존 값을 유지한다.
  // 서버/펌웨어 버전이 잠시 맞지 않아도 모든 임계값이 0으로 초기화되는 위험을 줄인다.
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
  // LED 상태 판단 로직이다.
  // 토양수분 부족은 작물 생육에 즉시 영향을 줄 수 있어 별도 상태(흰색 LED)로 가장 먼저 판정한다.
  if (soilMoisture < thresholds.soilMoistureMin) {
    return STATUS_SOIL_DRY;
  }

  // 온도/습도/조도 중 하나라도 장치별 임계값을 벗어나면 비정상 상태(빨간 LED)로 본다.
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
  // 상태 전환 때 이전 LED가 남아 있지 않도록 세 LED를 모두 끈 뒤 하나만 켠다.
  digitalWrite(LED_GREEN_PIN, LOW);
  digitalWrite(LED_RED_PIN, LOW);
  digitalWrite(LED_WHITE_PIN, LOW);
}

void updateStatusLED(FarmStatus status) {
  // LED 색상 규칙:
  // 녹색 = 모든 센서값 정상, 빨간색 = 온도/습도/조도 이상, 흰색 = 토양 건조.
  // 이 판단 기준은 서버에서 가져온 thresholds 값과 evaluateFarmStatus() 결과에 의해 결정된다.
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
  // 부팅 직후 Serial과 센서/핀을 초기화한다.
  // LED는 먼저 모두 꺼서 이전 전원 상태가 남아 보이지 않게 한다.
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
    // Wi-Fi가 연결된 경우에만 NTP를 시도한다.
    // 실패해도 이후 getTimestamp()가 time_not_set을 반환하고 서버 저장은 계속된다.
    syncTime();
  }
  printThresholds();
}

void loop() {
  // 메인 루프 흐름:
  // 1) Wi-Fi 상태 확인 및 재연결
  // 2) 서버 임계값 주기적 조회
  // 3) 센서값 측정
  // 4) 임계값으로 LED 상태 판단
  // 5) JSON 생성 후 Flask /api/sensor로 POST
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
    // 부팅 후 최초 1회, 이후 45초마다 서버 임계값을 가져온다.
    // 사용자가 대시보드에서 기준값을 바꾸면 다음 조회 시 ESP32 LED 판단에도 반영된다.
    lastThresholdFetchAt = now;
    thresholdFetchAttempted = true;
    fetchThresholdsFromServer();
  }

  String timestamp = getTimestamp();

  // DHT22 온도/습도 측정.
  // 실패하면 -1로 보내 서버/대시보드에서 비정상 데이터임을 확인할 수 있게 한다.
  float temperature = dht.readTemperature();
  float humidity = dht.readHumidity();

  // 토양 아날로그 원시값은 보정/진단용으로 함께 전송하고,
  // 디지털 출력은 센서 모듈 자체 임계값 상태를 확인하는 보조값이다.
  int soilRaw = analogRead(SOIL_AO_PIN);
  int soilDigital = digitalRead(SOIL_DO_PIN);
  int lightDigital = digitalRead(LIGHT_DO_PIN);

  if (isnan(temperature) || isnan(humidity)) {
    Serial.println("Failed to read from DHT sensor");
    temperature = -1;
    humidity = -1;
  }

  // 토양수분 변환.
  // 일반적으로 soilRaw 값이 클수록 건조, 작을수록 습하므로 4095 -> 0%, 0 -> 100%로 매핑한다.
  // 서버에는 사람이 이해하기 쉬운 soil_moisture(%)와 보정용 soil_raw를 같이 보낸다.
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
  // LED는 서버 저장 성공 여부와 무관하게 현재 측정값 기준으로 즉시 갱신한다.
  // 네트워크가 끊겨도 현장에서는 LED로 상태를 확인할 수 있다.
  updateStatusLED(status);
  Serial.print("LED state: ");
  Serial.println(farmStatusName(status));

  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;

    // Flask 서버의 센서 수집 endpoint로 전송한다.
    // Content-Type이 application/json이어야 Flask request.get_json()이 body를 dict로 해석한다.
    http.begin(serverUrl);
    http.setTimeout(HTTP_TIMEOUT_MS);
    http.addHeader("Content-Type", "application/json");

    String jsonData = "{";
    // JSON 필드명은 Flask의 validate_sensor_payload()와 sensor_service._COLUMNS 기준에 맞춘다.
    // 추가 진단값(soil_raw, soil_digital, light_digital)은 DB 컬럼에 존재하므로 함께 저장된다.
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

    // 정상 저장 시 Flask는 201을 반환한다.
    // 응답 body에는 서버가 받은 data가 들어 있어 Serial Monitor에서 전송 내용 확인에 사용할 수 있다.
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
