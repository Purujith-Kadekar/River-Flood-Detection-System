#!/usr/bin/env python3
"""
Raspberry Pi LoRa Gateway Daemon — River Flood Detection System
===============================================================

This process is headless (no Flask, no HTTP, no port bindings).

What it does:
  1. Listens for LoRa packets from the ESP32 sensor node.
  2. Each packet carries the RAW (pre-average) water level in cm.
  3. Applies its own 5-sample moving-average filter here on the Pi.
  4. Runs a 2-minute flood confirmation window (90% of readings above
     threshold → confirmed flood event).
  5. Detects rapid-rise conditions (>2 cm/min).
  6. Writes every filtered reading into the shared SQLite database
     that app.py reads for the dashboard.

Run order for demo:
  1.  python3 receiver.py       ← start first (initialises DB)
  2.  python3 Dashboard/app.py  ← start second
  3.  Power on ESP32            ← packets start arriving

Environment variables (all optional):
  FLOOD_DB_PATH          Override the SQLite file path
  LORA_SIMULATION_MODE   Set to "1" to generate synthetic data
                         (use when no real LoRa hardware is connected)
  LORA_DISABLE_IRQ       Set to "1" to force SPI polling instead of
                         GPIO edge-detect (try this first if Pi is not
                         receiving — fixes most IRQ wiring issues)

LoRa parameters (must match main.cpp exactly):
  Frequency : 433 MHz
  SF        : 7
  BW        : 125 kHz
  CR        : 4/5
  Sync Word : 0xB4

Packet format received from ESP32:
  "DATA:<water_level_cm>"
  Example: "DATA:12.3"

Pi SPI/GPIO wiring (Ra-02 SX1278 → Pi Zero 2W):
  Ra-02 VCC  → 3.3V  (Pin 1)
  Ra-02 GND  → GND   (Pin 6)
  Ra-02 SCK  → GPIO11 / SPI0_CLK  (Pin 23)
  Ra-02 MISO → GPIO9  / SPI0_MISO (Pin 21)
  Ra-02 MOSI → GPIO10 / SPI0_MOSI (Pin 19)
  Ra-02 NSS  → GPIO8  / SPI0_CE0  (Pin 24)  ← CS_PIN
  Ra-02 RST  → GPIO25              (Pin 22)  ← RESET_PIN
  Ra-02 DIO0 → GPIO24              (Pin 18)  ← IRQ_PIN
"""

from __future__ import annotations

import os
import re
import signal
import sqlite3
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Optional dependencies (SMS + dotenv) ─────────────────────────────────────
# Install with: pip3 install requests python-dotenv
try:
    import requests as _requests
    HTTP_AVAILABLE = True
except ImportError:
    _requests = None  # type: ignore[assignment]
    HTTP_AVAILABLE = False

try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(_env_path)
except ImportError:
    pass  # python-dotenv not installed; fall back to system env vars

# Textbee SMS credentials — read from .env or system environment
TEXTBEE_API_KEY      = os.getenv("TEXTBEE_API_KEY", "")
TEXTBEE_DEVICE_ID    = os.getenv("TEXTBEE_DEVICE_ID", "")
ALERT_PHONE_NUMBERS  = [
    n.strip() for n in os.getenv("ALERT_PHONE_NUMBERS", "").split(",") if n.strip()
]
SMS_ENABLED = bool(TEXTBEE_API_KEY and TEXTBEE_DEVICE_ID and ALERT_PHONE_NUMBERS)
SMS_COOLDOWN_SECONDS = 600   # 10 minutes between repeat SMS for same ongoing event
_sms_last_sent: dict = {}    # alert_type -> datetime of last send

# ── LoRaRF import (graceful fallback for dev machines without hardware) ──────
try:
    from LoRaRF import SX127x
    try:
        from LoRaRF import SX1278  # type: ignore
    except ImportError:
        SX1278 = None  # type: ignore[assignment]
    LORA_AVAILABLE = True
    LORA_IMPORT_ERROR = None
except ImportError as exc:
    try:
        from LoRaRF.SX127x import SX127x
        try:
            from LoRaRF.SX1278 import SX1278  # type: ignore
        except ImportError:
            SX1278 = None  # type: ignore[assignment]
        LORA_AVAILABLE = True
        LORA_IMPORT_ERROR = None
    except ImportError:
        SX127x = None  # type: ignore[assignment]
        SX1278 = None  # type: ignore[assignment]
        LORA_AVAILABLE = False
        LORA_IMPORT_ERROR = exc

try:
    from LoRaRF.base import LoRaSpi, LoRaGpio
    LORA_HAS_HELPERS = True
except ImportError:
    LoRaSpi = None  # type: ignore[assignment]
    LoRaGpio = None  # type: ignore[assignment]
    LORA_HAS_HELPERS = False

# ============================================================
# CONFIGURATION
# ============================================================

READING_INTERVAL_SECONDS   = 5
TWO_MINUTE_WINDOW_SECONDS  = 120
BUFFER_SIZE                = TWO_MINUTE_WINDOW_SECONDS // READING_INTERVAL_SECONDS  # 24
MOVING_WINDOW_SIZE         = 5

FLOOD_THRESHOLD_CM         = 15.0   # matches ESP32 FLOOD_THRESHOLD_CM
WARNING_THRESHOLD_CM       = 10.0
FLOOD_CONFIRMATION_PERCENT = 90     # % of 2-min window above threshold → confirmed flood
RAPID_RISE_CM_PER_MIN      = 2.0    # cm/min rise rate → rapid-rise alert

# LoRa hardware config — must match main.cpp exactly
LORA_FREQUENCY        = 433_000_000  # Hz
# Sync word derived from SHA256("RiverFloodDetectionDSCE_VTU_1BPRJ208_PurujithKadekar")
# Unique to this project — not shared with any tutorial or public network
LORA_SYNC_WORD        = 0xB4
LORA_SPREADING_FACTOR = 7
LORA_BANDWIDTH        = 125_000      # Hz
LORA_CODING_RATE      = 5           # 4/5
LORA_PREAMBLE_LENGTH  = 12
LORA_MAX_PAYLOAD_LENGTH = 64

# Pi GPIO/SPI wiring for Ra-02
SPI_BUS_ID   = 0
SPI_CS_ID    = 0
SPI_SPEED_HZ = 7_800_000
CS_PIN       = 8    # BCM GPIO8  (SPI0_CE0, physical pin 24)
RESET_PIN    = 25   # BCM GPIO25 (physical pin 22)
IRQ_PIN      = 24   # BCM GPIO24 (physical pin 18) — DIO0

SIMULATION_MODE  = os.getenv("LORA_SIMULATION_MODE", "0").lower() in {"1", "true", "yes", "on"}
LORA_DISABLE_IRQ = os.getenv("LORA_DISABLE_IRQ", "0").lower() in {"1", "true", "yes", "on"}

# ============================================================
# SMS ALERTS (Textbee)
# ============================================================

SMS_COOLDOWN_SECONDS = 600  # re-send SMS every 10 min during sustained flood
_last_sms_time: dict = {}   # alert_type → datetime of last SMS sent


def send_textbee_sms(alert_type: str, water_level: float, detail: str) -> None:
    """
    Send an SMS alert via Textbee to every number in ALERT_PHONE_NUMBERS.

    Runs in a daemon thread so the LoRa listener is never blocked.
    Credentials are loaded from .env (see project root .env file).

    .env keys required:
        TEXTBEE_API_KEY      — API key from app.textbee.dev/dashboard
        TEXTBEE_DEVICE_ID    — Device ID from same dashboard
        ALERT_PHONE_NUMBERS  — Comma-separated E.164 numbers: +91XXXXXXXXXX,+1XXXXXXXXXX
    """
    if not SMS_ENABLED:
        if not TEXTBEE_API_KEY:
            print("[SMS] Skipped — TEXTBEE_API_KEY not set in .env")
        return

    if not HTTP_AVAILABLE:
        print("[SMS] Skipped — 'requests' library not installed (pip3 install requests)")
        return

    # Cooldown check — avoid spamming SMS every 5 seconds for ongoing flood
    now_dt = datetime.now()
    last = _sms_last_sent.get(alert_type)
    if last and (now_dt - last).total_seconds() < SMS_COOLDOWN_SECONDS:
        remaining = int(SMS_COOLDOWN_SECONDS - (now_dt - last).total_seconds())
        print(f"[SMS] Cooldown active for {alert_type} — next SMS in {remaining}s")
        return
    _sms_last_sent[alert_type] = now_dt

    # Cooldown check — don't spam the same alert type; re-send after SMS_COOLDOWN_SECONDS
    now = datetime.now()
    last_sent = _last_sms_time.get(alert_type)
    if last_sent and (now - last_sent).total_seconds() < SMS_COOLDOWN_SECONDS:
        remaining = int(SMS_COOLDOWN_SECONDS - (now - last_sent).total_seconds())
        print(f"[SMS] Cooldown active for {alert_type} — next SMS in {remaining}s")
        return
    _last_sms_time[alert_type] = now

    sms_body = (
        f"\u26a0\ufe0f FLOOD ALERT\n"
        f"Type    : {alert_type}\n"
        f"Level   : {water_level:.1f} cm\n"
        f"Detail  : {detail}\n"
        f"Time    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Action  : Check the river immediately!"
    )

    # ── Textbee API call (verified against official docs Jan 2026) ──
    # Endpoint : POST https://api.textbee.dev/api/v1/gateway/devices/{DEVICE_ID}/send-sms
    # Auth     : header "x-api-key"  (NOT "Authorization: Bearer ...")
    # Body key : "recipients" (array)  (NOT "receivers" / "to")
    # Bulk     : all numbers in one call — Textbee sends to each recipient
    url = f"https://api.textbee.dev/api/v1/gateway/devices/{TEXTBEE_DEVICE_ID}/send-sms"
    headers = {
        "x-api-key":    TEXTBEE_API_KEY,   # exact header name Textbee requires
        "Content-Type": "application/json",
    }
    payload = {
        "recipients": ALERT_PHONE_NUMBERS,  # list of E.164 numbers, sent in ONE call
        "message":    sms_body,
    }

    def _fire() -> None:
        try:
            # Step 1: Send the SMS batch
            resp = _requests.post(url, json=payload, headers=headers, timeout=10)
            if resp.status_code not in {200, 201}:
                print(f"[SMS] Send failed: HTTP {resp.status_code} — {resp.text[:200]}")
                return

            data = resp.json().get("data", {})
            batch_id = data.get("_id", "")
            print(f"[SMS] Batch queued: id={batch_id}  recipients={ALERT_PHONE_NUMBERS}  status={data.get('status','?')}")

            if not batch_id:
                return

            # Step 2: Poll the batch status after 15 s (phone needs time to send)
            # Endpoint: GET /gateway/devices/{DEVICE_ID}/sms-batch/{BATCH_ID}
            time.sleep(15)
            batch_url = (f"https://api.textbee.dev/api/v1/gateway/devices"
                         f"/{TEXTBEE_DEVICE_ID}/sms-batch/{batch_id}")
            try:
                check = _requests.get(batch_url, headers={"x-api-key": TEXTBEE_API_KEY}, timeout=10)
                if check.status_code == 200:
                    messages = check.json().get("data", {}).get("messages", [])
                    for msg in messages:
                        recipient = msg.get("recipient", "?")
                        status    = msg.get("status", "?")
                        err       = msg.get("errorMessage", "")
                        if status == "DELIVERED":
                            print(f"[SMS] DELIVERED to {recipient} ✓")
                        elif status == "SENT":
                            print(f"[SMS] SENT to {recipient} (awaiting delivery receipt)")
                        elif status == "FAILED":
                            print(f"[SMS] FAILED for {recipient}: {err}")
                        else:
                            print(f"[SMS] {recipient} → {status}")
                else:
                    print(f"[SMS] Batch check HTTP {check.status_code}")
            except Exception as exc:
                print(f"[SMS] Batch status check error: {exc}")

        except Exception as exc:
            print(f"[SMS] Network error: {exc}")

    threading.Thread(target=_fire, daemon=True).start()


# ============================================================
# DATABASE PATH RESOLUTION
# ============================================================

def resolve_database_path() -> Path:
    """Find a writable location for the shared SQLite database."""
    env_path = os.getenv("FLOOD_DB_PATH")
    candidates: list[Path] = []

    if env_path:
        candidates.append(Path(env_path))

    script_dir = Path(__file__).resolve().parent
    candidates.extend([
        script_dir / "Dashboard" / "instance" / "flood_detection.db",
        script_dir.parent / "Dashboard" / "instance" / "flood_detection.db",
        Path.cwd() / "Dashboard" / "instance" / "flood_detection.db",
        Path.home() / "Dashboard" / "instance" / "flood_detection.db",
        script_dir / "flood_detection.db",
        Path.cwd() / "flood_detection.db",
        Path.home() / "flood_detection.db",
    ])

    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            test = candidate.parent / ".db_write_test"
            test.write_text("ok", encoding="utf-8")
            test.unlink()
            return candidate
        except Exception:
            continue

    return Path.home() / "flood_detection.db"


DATABASE_PATH = resolve_database_path()
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
print(f"[DB] Using database: {DATABASE_PATH}")

# ============================================================
# DATABASE HELPERS
# ============================================================

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_database() -> None:
    """Create tables if they don't exist.

    Column names here MUST match the SQLAlchemy model in app.py:
      WaterLevel  → water_level (Float), river_name (String)
      FloodEvent  → max_water_level (Float)   ← critical: was 'max_level' - FIXED
      AlertLog    → alert_type, water_level, message
    """
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS water_level (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                water_level REAL    NOT NULL,
                river_name  TEXT    NOT NULL DEFAULT 'Gateway'
            )
        """)
        # IMPORTANT: column is 'max_water_level' to match app.py ORM model
        conn.execute("""
            CREATE TABLE IF NOT EXISTS flood_event (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time      TEXT    NOT NULL,
                end_time        TEXT,
                max_water_level REAL    NOT NULL,
                river_name      TEXT    DEFAULT 'Gateway',
                severity        TEXT,
                description     TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                alert_type  TEXT    NOT NULL,
                water_level REAL,
                message     TEXT
            )
        """)
        conn.commit()
    print("[DB] Tables verified/created")


def save_reading_to_db(water_level_cm: float) -> Optional[int]:
    """Insert a filtered water-level reading into the shared table."""
    try:
        with db_connect() as conn:
            cursor = conn.execute(
                "INSERT INTO water_level (timestamp, water_level, river_name) VALUES (?, ?, ?)",
                (datetime.now().isoformat(timespec="seconds"),
                 float(water_level_cm),
                 "Gateway"),
            )
            conn.commit()
            return cursor.lastrowid
    except Exception as exc:
        print(f"[DB] ERROR saving reading: {exc}")
        return None


def log_alert(alert_type: str, water_level: float, message: str) -> None:
    try:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO alert_log (timestamp, alert_type, water_level, message) VALUES (?, ?, ?, ?)",
                (datetime.now().isoformat(timespec="seconds"),
                 alert_type,
                 float(water_level),
                 message),
            )
            conn.commit()
    except Exception as exc:
        print(f"[DB] ERROR logging alert: {exc}")

    # Fire SMS for critical events only (non-blocking, runs in daemon thread)
    if alert_type in {"FLOOD", "RAPID_RISE"}:
        send_textbee_sms(alert_type, water_level, message)


def create_flood_event(max_water_level: float, description: str) -> Optional[int]:
    """Insert a new flood event row. Uses 'max_water_level' to match app.py."""
    try:
        with db_connect() as conn:
            cursor = conn.execute(
                """INSERT INTO flood_event
                   (start_time, max_water_level, severity, description)
                   VALUES (?, ?, ?, ?)""",
                (datetime.now().isoformat(timespec="seconds"),
                 float(max_water_level),
                 "Moderate",
                 description),
            )
            conn.commit()
            return cursor.lastrowid
    except Exception as exc:
        print(f"[DB] ERROR creating flood event: {exc}")
        return None


def end_flood_event(event_id: int) -> None:
    try:
        with db_connect() as conn:
            conn.execute(
                "UPDATE flood_event SET end_time = ? WHERE id = ?",
                (datetime.now().isoformat(timespec="seconds"), int(event_id)),
            )
            conn.commit()
            print(f"[DB] Flood event #{event_id} ended")
    except Exception as exc:
        print(f"[DB] ERROR ending flood event: {exc}")

# ============================================================
# FLOOD DETECTION ENGINE
# ============================================================

class FloodDetectionEngine:
    """
    2-minute temporal confirmation filter + rapid-rise detector.

    Logic:
      - Keeps the last 24 readings (120 s / 5 s = 24 slots).
      - If ≥90% of those readings are above FLOOD_THRESHOLD_CM
        → status = CONFIRMED_FLOOD  (logged to DB as a flood_event).
      - If 50–90% above threshold → status = WARNING (after 3 occurrences).
      - When levels return below 50% → status = NORMAL, flood event closed.
    """

    def __init__(self):
        self.readings_buffer: deque = deque(maxlen=BUFFER_SIZE)
        self.current_status   = "NORMAL"
        self.confirmed_flood_id: Optional[int] = None
        self.rapid_rise_active = False
        self.transient_count   = 0
        self.last_sms_time: Optional[datetime] = None   # cooldown tracker
        self.SMS_COOLDOWN_MINUTES = 10  # re-send SMS every 10 min if still flooding

    def add_reading(self, water_level_cm: float, rssi: int) -> dict:
        ts = datetime.now()
        self.readings_buffer.append({
            "timestamp":   ts,
            "water_level": water_level_cm,
            "rssi":        rssi,
        })
        self._detect_flood(water_level_cm)
        self._detect_rapid_rise()
        return self.get_current_status()

    def _detect_flood(self, latest_level: float) -> None:
        if len(self.readings_buffer) < 5:
            return  # need at least 5 readings before making any call

        cutoff = datetime.now() - timedelta(seconds=TWO_MINUTE_WINDOW_SECONDS)
        recent = [r for r in self.readings_buffer if r["timestamp"] >= cutoff]
        if len(recent) < 5:
            return

        above      = sum(1 for r in recent if r["water_level"] >= FLOOD_THRESHOLD_CM)
        total      = len(recent)
        pct_above  = (above / total) * 100

        if pct_above >= FLOOD_CONFIRMATION_PERCENT:
            if self.current_status != "CONFIRMED_FLOOD":
                # First time crossing into confirmed flood — create DB event
                self.current_status = "CONFIRMED_FLOOD"
                event_id = create_flood_event(
                    max_water_level=latest_level,
                    description=f"2-min verification: {pct_above:.0f}% of readings above "
                                f"{FLOOD_THRESHOLD_CM} cm threshold",
                )
                self.confirmed_flood_id = event_id
                print(f"[FLOOD] *** CONFIRMED FLOOD *** {pct_above:.0f}% above threshold "
                      f"(event #{event_id})")
            # Call log_alert every cycle — SMS function has its own cooldown
            # so it won't actually send more than once per SMS_COOLDOWN_SECONDS
            log_alert("FLOOD", latest_level,
                      f"Confirmed flood: {pct_above:.0f}% above threshold")

        elif pct_above >= 50:
            if self.current_status == "NORMAL":
                self.transient_count += 1
                log_alert("TRANSIENT", latest_level,
                          f"Transient warning #{self.transient_count}: {pct_above:.0f}% above")
                print(f"[WARN] Transient #{self.transient_count}: {pct_above:.0f}% above threshold")
                if self.transient_count >= 3:
                    self.current_status = "WARNING"
                    log_alert("WARNING", latest_level,
                              "Elevated levels sustained — potential flood")
                    print("[WARN] Status → WARNING")

        else:
            if self.current_status in {"CONFIRMED_FLOOD", "WARNING"}:
                was_flood = self.current_status == "CONFIRMED_FLOOD"
                self.current_status  = "NORMAL"
                self.transient_count = 0
                self.rapid_rise_active = False
                if was_flood and self.confirmed_flood_id:
                    end_flood_event(self.confirmed_flood_id)
                    self.confirmed_flood_id = None
                self.last_sms_time = None   # reset cooldown for next flood event
                log_alert("RECOVERY", latest_level,
                          "Water levels returned to normal")
                print("[RECOVERY] Status → NORMAL")

    def _detect_rapid_rise(self) -> None:
        if len(self.readings_buffer) < 6:
            return
        oldest = self.readings_buffer[0]
        newest = self.readings_buffer[-1]
        time_diff_min = (newest["timestamp"] - oldest["timestamp"]).total_seconds() / 60.0
        if time_diff_min < 1.0:
            return
        rate = (newest["water_level"] - oldest["water_level"]) / time_diff_min
        if rate >= RAPID_RISE_CM_PER_MIN:
            if not self.rapid_rise_active:
                self.rapid_rise_active = True
                log_alert("RAPID_RISE", newest["water_level"],
                          f"Rapid rise: {rate:.1f} cm/min")
                print(f"[WARN] RAPID RISE: {rate:.1f} cm/min")
        elif rate < 0 and self.rapid_rise_active:
            self.rapid_rise_active = False
            print("[INFO] Rapid rise ended — levels falling")

    def get_current_status(self) -> dict:
        return {
            "status":             self.current_status,
            "rapid_rise":         self.rapid_rise_active,
            "transient_warnings": self.transient_count,
            "buffer_count":       len(self.readings_buffer),
            "flood_event_id":     self.confirmed_flood_id,
        }

# ============================================================
# LORA RECEIVER
# ============================================================

class LoRaReceiver:
    """
    Headless LoRa receive worker using LoRaRF SX127x.

    The moving-average filter lives here (not on the ESP32) so each
    side has exactly one stage of smoothing and there is no double-lag.
    """

    def __init__(self):
        self.running    = False
        self.thread: Optional[threading.Thread] = None
        self.radio      = None
        self._irq_polling = False
        # 5-sample moving average of RAW readings from ESP32
        self.moving_window: deque = deque(maxlen=MOVING_WINDOW_SIZE)
        self.flood_engine = FloodDetectionEngine()

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self):
        if self.running:
            print("[LORA] Already running")
            return
        if not LORA_AVAILABLE and not SIMULATION_MODE:
            print("[LORA] ERROR: LoRaRF not available and simulation is off.")
            if LORA_IMPORT_ERROR:
                print(f"[LORA] Import error: {LORA_IMPORT_ERROR}")
            print("[LORA] Tip: run with LORA_SIMULATION_MODE=1 to test without hardware.")
            return
        self.running = True
        self.thread  = threading.Thread(target=self._listen_loop, daemon=True)
        self.thread.start()
        print("[LORA] Listener thread started")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=3)
        if self.radio is not None:
            try:
                self.radio.end()
            except Exception:
                pass
        print("[LORA] Listener stopped")

    # ── radio init ─────────────────────────────────────────────────────────

    def _build_radio(self):
        self.radio  = None
        radio_class = SX1278 if SX1278 is not None else SX127x
        if radio_class is None:
            raise RuntimeError("LoRaRF SX127x class not available")

        effective_irq   = -1 if LORA_DISABLE_IRQ else IRQ_PIN
        self._irq_polling = effective_irq < 0
        irq_label       = "DISABLED (polling)" if effective_irq < 0 else f"BCM GPIO {IRQ_PIN}"

        print(f"[LORA] SPI bus {SPI_BUS_ID}:{SPI_CS_ID}  "
              f"RST=BCM{RESET_PIN}  DIO0={irq_label}")

        self.radio = radio_class()
        began = None

        # Try the 4-arg begin() first (newer LoRaRF builds)
        try:
            began = self.radio.begin(SPI_BUS_ID, SPI_CS_ID, RESET_PIN, effective_irq)
        except TypeError:
            pass

        # Fall back to no-arg begin() with LoRaSpi/LoRaGpio helpers
        if began is None:
            if not LORA_HAS_HELPERS:
                raise RuntimeError(
                    "LoRaRF helpers (LoRaSpi/LoRaGpio) not available and "
                    "direct begin(bus,cs,rst,irq) is unsupported by this build."
                )
            spi   = LoRaSpi(SPI_BUS_ID, SPI_CS_ID)
            cs    = LoRaGpio(0, CS_PIN)
            reset = LoRaGpio(0, RESET_PIN)
            irq   = LoRaGpio(0, IRQ_PIN)
            self.radio = radio_class(spi, cs, reset, irq)
            began = self.radio.begin()

        if not began:
            raise RuntimeError(
                "radio.begin() returned False — check SPI wiring and GPIO numbers.\n"
                "Verify: CS=BCM8, RST=BCM25, DIO0=BCM24 match your physical wiring.\n"
                "Quick test: run with LORA_DISABLE_IRQ=1 to rule out IRQ pin issues."
            )

        # Optional SPI speed
        if hasattr(self.radio, "setSpiSpeed"):
            try:
                self.radio.setSpiSpeed(SPI_SPEED_HZ)
            except Exception:
                pass

        # Apply LoRa parameters (must match main.cpp)
        self.radio.setFrequency(LORA_FREQUENCY)
        self.radio.setRxGain(self.radio.RX_GAIN_POWER_SAVING, self.radio.RX_GAIN_AUTO)
        self.radio.setLoRaModulation(LORA_SPREADING_FACTOR, LORA_BANDWIDTH, LORA_CODING_RATE)
        self.radio.setLoRaPacket(
            self.radio.HEADER_EXPLICIT,
            LORA_PREAMBLE_LENGTH,
            LORA_MAX_PAYLOAD_LENGTH,
            True,   # CRC enabled
        )
        self.radio.setSyncWord(LORA_SYNC_WORD)

        print(f"[LORA] Configured: {LORA_FREQUENCY/1e6:.0f} MHz  "
              f"SF{LORA_SPREADING_FACTOR}  BW{LORA_BANDWIDTH/1e3:.0f}kHz  "
              f"CR4/{LORA_CODING_RATE}  Sync=0x{LORA_SYNC_WORD:02X}")

    # ── listen loop ────────────────────────────────────────────────────────

    def _listen_loop(self):
        try:
            self._build_radio()
        except Exception as exc:
            print(f"[LORA] Init error: {exc}")
            if SIMULATION_MODE:
                print("[LORA] Falling back to simulation mode")
                self._simulation_loop()
            return

        print("[LORA] Listening for packets...")
        try:
            self._request_rx()
        except Exception as exc:
            print(f"[LORA] Could not enter RX mode: {exc}")
            return

        while self.running:
            try:
                if self._irq_polling:
                    if not self.radio.wait(0.2):
                        time.sleep(0.02)
                        continue
                if self.radio.available() > 0:
                    raw_text = self._read_packet_bytes()
                    rssi     = self.radio.packetRssi()  # true received RSSI
                    if raw_text:
                        self._process_packet(raw_text, rssi)
                    self.radio.standby()
                    self._request_rx()
                else:
                    time.sleep(0.05)
            except Exception as exc:
                print(f"[LORA] RX error: {exc}")
                time.sleep(0.5)
                try:
                    self.radio.standby()
                    self._request_rx()
                except Exception:
                    pass

    def _request_rx(self) -> None:
        try:
            self.radio.request(self.radio.RX_CONTINUOUS)
        except RuntimeError as exc:
            if "Failed to add edge detection" not in str(exc):
                raise
            print("[LORA] IRQ edge-detect failed — switching to polling mode")
            print("[LORA] Tip: re-run with LORA_DISABLE_IRQ=1 to avoid this")
            self._irq_polling = True
            try:
                if hasattr(self.radio, "setPins"):
                    self.radio.setPins(RESET_PIN, -1)
            except Exception:
                pass
            self.radio.request(self.radio.RX_CONTINUOUS)

    def _read_packet_bytes(self) -> str:
        payload = bytearray()
        while self.radio.available() > 0:
            payload.append(self.radio.read())
        return payload.decode("utf-8", errors="ignore").strip()

    # ── packet processing ──────────────────────────────────────────────────

    def _process_packet(self, packet_str: str, rssi: int) -> None:
        print(f"[LORA] RX: \"{packet_str}\"  RSSI={rssi} dBm")

        # Expected format: "DATA:<float>"  e.g. "DATA:12.3"
        match = re.match(r"^DATA:([+-]?\d+(?:\.\d+)?)$", packet_str)
        if not match:
            print(f"[LORA] Unrecognised packet format — ignored: {packet_str!r}")
            return

        raw_water_level = float(match.group(1))

        # Apply 5-sample moving average on the Pi side
        self.moving_window.append(raw_water_level)
        filtered_level = sum(self.moving_window) / len(self.moving_window)

        # Run flood detection logic
        status_dict = self.flood_engine.add_reading(filtered_level, rssi)
        status      = status_dict["status"]

        # Persist to shared database
        save_reading_to_db(filtered_level)

        print(f"[DATA] Raw={raw_water_level:.1f} cm  "
              f"Avg(n={len(self.moving_window)})={filtered_level:.1f} cm  "
              f"RSSI={rssi} dBm  Status={status}")

    # ── simulation mode (no hardware) ─────────────────────────────────────

    def _simulation_loop(self):
        import random
        print("[SIM] Generating synthetic sensor data every 5 s")
        base = 8.0
        while self.running:
            level = base + random.uniform(-3, 6)
            if random.random() < 0.1:
                level = random.uniform(15, 18)  # occasional flood spike
            level = max(0.0, level)
            rssi  = random.randint(-100, -80)

            self.moving_window.append(level)
            filtered = sum(self.moving_window) / len(self.moving_window)
            status   = self.flood_engine.add_reading(filtered, rssi)["status"]
            save_reading_to_db(filtered)
            print(f"[SIM] Raw={level:.1f}  Avg={filtered:.1f}  Status={status}")
            time.sleep(READING_INTERVAL_SECONDS)

# ============================================================
# MAIN
# ============================================================

lora_receiver: Optional[LoRaReceiver] = None


def signal_handler(sig, frame):
    global lora_receiver
    print("\n[SHUTDOWN] Stopping receiver...")
    if lora_receiver:
        lora_receiver.stop()
    sys.exit(0)


def main():
    global lora_receiver

    print("=" * 60)
    print("  RASPBERRY PI FLOOD DETECTION GATEWAY")
    print("  LoRaRF SX1278 + SQLite")
    print("=" * 60)
    print()
    print(f"[CONFIG] Flood threshold       : {FLOOD_THRESHOLD_CM} cm")
    print(f"[CONFIG] Warning threshold     : {WARNING_THRESHOLD_CM} cm")
    print(f"[CONFIG] 2-min buffer size     : {BUFFER_SIZE} readings")
    print(f"[CONFIG] Confirmation required : {FLOOD_CONFIRMATION_PERCENT}%")
    print(f"[CONFIG] Rapid rise threshold  : >{RAPID_RISE_CM_PER_MIN} cm/min")
    print(f"[CONFIG] Simulation mode       : {'ON' if SIMULATION_MODE else 'off'}")
    print(f"[CONFIG] IRQ disabled (polling): {'YES' if LORA_DISABLE_IRQ else 'no'}")
    print(f"[CONFIG] Database path         : {DATABASE_PATH}")
    print()

    init_database()

    lora_receiver = LoRaReceiver()
    lora_receiver.start()

    if not lora_receiver.running:
        print("[FATAL] Receiver did not start — check LoRaRF install and wiring")
        sys.exit(1)

    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print()
    print("=" * 60)
    print("  GATEWAY RUNNING")
    print("  Dashboard: cd Dashboard && python3 app.py")
    print("  Stop     : Ctrl+C")
    print("=" * 60)
    print()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if lora_receiver:
            lora_receiver.stop()


if __name__ == "__main__":
    main()
