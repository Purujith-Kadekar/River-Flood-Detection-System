/**
 * ESP32-S3 Water Level Sensor with LoRa Communication
 *
 * Hardware: Edgehax ESP32-S3-WROOM-N16R8 (S3-Pro Dev Kit)
 *           HC-SR04 Ultrasonic Sensor
 *           LoRa Ra-02 SX1278 @ 433 MHz
 *           Active Piezo Buzzer
 *
 * Logic (exactly as intended):
 *   - Collect 5 readings one by one, every 5 seconds each
 *   - After the 5th reading, average all 5
 *   - If average >= FLOOD_THRESHOLD_CM → buzz
 *   - If average <  FLOOD_THRESHOLD_CM → stop buzzing
 *   - Reset slot index to 0 and collect the next fresh batch of 5
 *
 *   This is a BATCH window (not rolling). Every decision is based on
 *   5 completely fresh readings. One gust of wind can spike at most
 *   1 of the 5 slots — you need sustained high water across all 5
 *   to trigger the alarm. Robust against transient disturbances.
 *
 * Pin Assignments (Edgehax S3-Pro N16R8 pinout verified):
 *   HC-SR04 : TRIG=GPIO5, ECHO=GPIO6
 *   Buzzer  : GPIO7 (active buzzer +ve terminal)
 *   SX1278  : SCK=GPIO12, MISO=GPIO13, MOSI=GPIO11
 *             CS=GPIO10,  RST=GPIO14,  DIO0=GPIO9
 *
 * LoRa Parameters (must match receiver.py exactly):
 *   Frequency : 433 MHz  |  SF7  |  BW 125 kHz  |  CR 4/5  |  Sync 0xB4
 *
 * Author: River Flood Detection System — 2026
 */

#include <SPI.h>
#include <LoRa.h>

// ============================================
// PIN DEFINITIONS
// ============================================

#define TRIGGER_PIN    5
#define ECHO_PIN       6
#define BUZZER_PIN     7

#define LORA_SCK       12
#define LORA_MISO      13
#define LORA_MOSI      11
#define LORA_CS        10
#define LORA_RST       14
#define LORA_DIO0       9

// ============================================
// CONFIGURATION
// ============================================

#define EMPTY_CONTAINER_HEIGHT_CM  25.0f   // sensor → dry bottom distance
#define FLOOD_THRESHOLD_CM         15.0f   // alarm triggers above this average

#define WINDOW_SIZE                5        // batch size: decide after 5 readings
#define TX_INTERVAL_MS             5000UL   // one reading every 5 seconds

// HC-SR04 valid echo duration range
// 1 cm  →   58 µs  (minimum meaningful distance)
// 68 cm → 3965 µs  (well above the 25 cm bucket, generous upper bound)
// 0     → timeout  (always invalid — no echo received)
#define HC_SR04_MIN_DURATION_US    58UL
#define HC_SR04_MAX_DURATION_US    4000UL

// LoRa
#define LORA_FREQUENCY             433E6
#define LORA_TX_POWER              17
#define LORA_BANDWIDTH             125E3
#define LORA_SPREADING_FACTOR      7
#define LORA_CODING_RATE           5
// Sync word derived from SHA256("RiverFloodDetectionDSCE_VTU_1BPRJ208_PurujithKadekar")
// Unique to this project — not shared with any tutorial or public network
#define LORA_SYNC_WORD             0xB4

// ============================================
// GLOBALS
// ============================================

float         readingWindow[WINDOW_SIZE];   // batch of 5 readings
int           slotIndex    = 0;             // which slot to fill next (0–4)
unsigned long lastTxTime   = 0;
bool          buzzerActive = false;

// ============================================
// BATCH AVERAGE
// ============================================

/**
 * Average exactly WINDOW_SIZE slots.
 * Called only when all 5 slots are freshly filled (slotIndex == WINDOW_SIZE).
 */
float calculateBatchAverage() {
    float sum = 0.0f;
    for (int i = 0; i < WINDOW_SIZE; i++) {
        sum += readingWindow[i];
    }
    return sum / (float)WINDOW_SIZE;
}

// ============================================
// HC-SR04
// ============================================

/**
 * Single pulse → distance in cm.
 * Returns -1.0 on timeout or out-of-range (caller must skip this reading).
 */
float readUltrasonicDistance() {
    digitalWrite(TRIGGER_PIN, LOW);
    delayMicroseconds(2);
    digitalWrite(TRIGGER_PIN, HIGH);
    delayMicroseconds(10);
    digitalWrite(TRIGGER_PIN, LOW);

    long duration = pulseIn(ECHO_PIN, HIGH, 30000UL);

    if (duration == 0 ||
        duration < (long)HC_SR04_MIN_DURATION_US ||
        duration > (long)HC_SR04_MAX_DURATION_US) {
        return -1.0f;
    }

    return (duration * 0.0343f) / 2.0f;
}

/**
 * Take 3 shots, return median of valid ones.
 * Returns -1.0 if fewer than 2 valid shots received.
 */
float readFilteredDistance() {
    float shots[3];
    int   validCount = 0;

    for (int i = 0; i < 3; i++) {
        float d = readUltrasonicDistance();
        if (d > 0.0f) shots[validCount++] = d;
        delay(30);
    }

    if (validCount < 2) return -1.0f;

    // Sort and return median
    for (int i = 0; i < validCount - 1; i++) {
        for (int j = i + 1; j < validCount; j++) {
            if (shots[i] > shots[j]) {
                float tmp = shots[i]; shots[i] = shots[j]; shots[j] = tmp;
            }
        }
    }
    return shots[validCount / 2];
}

// ============================================
// BUZZER
// ============================================

void triggerBuzzer() {
    if (!buzzerActive) {
        buzzerActive = true;
        digitalWrite(BUZZER_PIN, HIGH);
        Serial.println("[ALARM] BUZZER ON");
    }
}

void stopBuzzer() {
    if (buzzerActive) {
        buzzerActive = false;
        digitalWrite(BUZZER_PIN, LOW);
        Serial.println("[ALARM] Buzzer OFF");
    }
}

// ============================================
// LORA
// ============================================

bool initLoRa() {
    Serial.println("[LORA] Initializing...");
    SPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_CS);
    LoRa.setPins(LORA_CS, LORA_RST, LORA_DIO0);

    if (!LoRa.begin(LORA_FREQUENCY)) {
        Serial.println("[LORA] ERROR: Failed to start!");
        return false;
    }

    LoRa.setTxPower(LORA_TX_POWER);
    LoRa.setSignalBandwidth(LORA_BANDWIDTH);
    LoRa.setSpreadingFactor(LORA_SPREADING_FACTOR);
    LoRa.setCodingRate4(LORA_CODING_RATE);
    LoRa.setSyncWord(LORA_SYNC_WORD);

    Serial.printf("[LORA] Ready on %.0f MHz  SF%d  BW%.0fkHz  CR4/%d  Sync=0x%02X\n",
                  LORA_FREQUENCY / 1E6, LORA_SPREADING_FACTOR,
                  LORA_BANDWIDTH / 1E3, LORA_CODING_RATE, LORA_SYNC_WORD);
    return true;
}

void sendLoRaPacket(float waterLevelCm) {
    char packet[32];
    snprintf(packet, sizeof(packet), "DATA:%.1f", waterLevelCm);
    LoRa.beginPacket();
    LoRa.print(packet);
    LoRa.endPacket();
    Serial.printf("[LORA] TX → \"%s\"\n", packet);
}

// ============================================
// SETUP
// ============================================

void setup() {
    Serial.begin(115200);
    delay(1000);

    Serial.println();
    Serial.println("==========================================");
    Serial.println("  River Flood Detection — ESP32-S3 Node");
    Serial.println("==========================================");
    Serial.printf("  Batch size      : %d readings\n", WINDOW_SIZE);
    Serial.printf("  Sample interval : %lu ms\n", TX_INTERVAL_MS);
    Serial.printf("  Empty height    : %.1f cm\n", EMPTY_CONTAINER_HEIGHT_CM);
    Serial.printf("  Flood threshold : %.1f cm\n", FLOOD_THRESHOLD_CM);
    Serial.println("==========================================");

    pinMode(TRIGGER_PIN, OUTPUT);
    pinMode(ECHO_PIN,    INPUT);
    pinMode(BUZZER_PIN,  OUTPUT);
    digitalWrite(BUZZER_PIN, LOW);

    // Clear the window so it always starts fresh
    for (int i = 0; i < WINDOW_SIZE; i++) readingWindow[i] = 0.0f;
    slotIndex = 0;

    if (!initLoRa()) {
        Serial.println("[FATAL] LoRa init failed — halting");
        while (true) delay(1000);
    }

    Serial.println("[SYS] Collecting first batch — decision after 5 readings (25 s)");
    lastTxTime = millis();
}

// ============================================
// LOOP
// ============================================

void loop() {
    unsigned long now = millis();

    if (now - lastTxTime < TX_INTERVAL_MS) {
        delay(10);
        return;
    }
    lastTxTime = now;

    // ── 1. Read sensor ──────────────────────────────────────────────────
    float distanceCm = readFilteredDistance();

    if (distanceCm < 0.0f) {
        // All shots invalid (timeout / out-of-range).
        // Do not count this as a slot — slotIndex stays the same.
        // The batch timer still advances so we try again in 5 s.
        Serial.println("[SENSOR] Bad read — not counted, retrying next cycle");
        return;
    }

    // ── 2. Convert distance → water level ──────────────────────────────
    float waterLevel = EMPTY_CONTAINER_HEIGHT_CM - distanceCm;
    if (waterLevel < 0.0f) waterLevel = 0.0f;

    // ── 3. Store into the current batch slot ────────────────────────────
    readingWindow[slotIndex] = waterLevel;
    slotIndex++;

    Serial.printf("[BATCH] Slot %d/%d filled → %.1f cm  (distance=%.1f cm)\n",
                  slotIndex, WINDOW_SIZE, waterLevel, distanceCm);

    // Send each individual reading to Pi as it arrives
    sendLoRaPacket(waterLevel);

    // ── 4. If batch is not yet full, wait for more readings ─────────────
    if (slotIndex < WINDOW_SIZE) {
        Serial.printf("[BATCH] Waiting for %d more reading(s)...\n",
                      WINDOW_SIZE - slotIndex);
        return;
    }

    // ── 5. Batch complete — average all 5 slots ─────────────────────────
    float avgLevel = calculateBatchAverage();

    Serial.println("========================================");
    Serial.printf("[BATCH] Complete! Readings: ");
    for (int i = 0; i < WINDOW_SIZE; i++) {
        Serial.printf("%.1f", readingWindow[i]);
        if (i < WINDOW_SIZE - 1) Serial.print(", ");
    }
    Serial.println();
    Serial.printf("[BATCH] Average = %.1f cm  |  Threshold = %.1f cm\n",
                  avgLevel, FLOOD_THRESHOLD_CM);

    // ── 6. Buzz or stop based on batch average ──────────────────────────
    if (avgLevel >= FLOOD_THRESHOLD_CM) {
        triggerBuzzer();
        Serial.printf("[ALERT] FLOOD DETECTED! Avg %.1f cm >= %.1f cm\n",
                      avgLevel, FLOOD_THRESHOLD_CM);
    } else {
        stopBuzzer();
        Serial.printf("[SAFE]  Avg %.1f cm < %.1f cm — no flood\n",
                      avgLevel, FLOOD_THRESHOLD_CM);
    }

    // ── 7. Reset slot index — next 5 readings form a fresh batch ────────
    slotIndex = 0;
    Serial.println("[BATCH] Reset — collecting next batch of 5");
    Serial.println("========================================");
}

// ============================================
// END
// ============================================
/*
 * COMPILATION (PlatformIO):
 *
 * [env:esp32s3]
 * platform = espressif32
 * board = esp32-s3-devkitc-1
 * framework = arduino
 * monitor_speed = 115200
 * lib_deps = sandeepmistry/LoRa@^0.8.0
 *
 * WIRING:
 *   HC-SR04 VCC→5V  GND→GND  TRIG→GPIO5  ECHO→GPIO6
 *   SX1278  VCC→3.3V GND→GND SCK→GPIO12 MISO→GPIO13
 *           MOSI→GPIO11 NSS→GPIO10 RST→GPIO14 DIO0→GPIO9
 *   Buzzer  (+)→GPIO7  (-)→GND
 */
