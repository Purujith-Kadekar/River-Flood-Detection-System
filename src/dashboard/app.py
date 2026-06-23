"""
River Flood Detection Dashboard — Flask App
============================================

Run AFTER receiver.py has started (receiver.py initialises the DB).

  python3 receiver.py   # terminal 1
  python3 app.py        # terminal 2

Access: http://<pi-ip>:5000
"""

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from pathlib import Path
import os
import random

app = Flask(__name__)

# ── Database ──────────────────────────────────────────────────────────────────
# Points to the same file that receiver.py writes to.
DATABASE_PATH = Path(__file__).resolve().with_name("instance").joinpath("flood_detection.db")
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

app.config["SQLALCHEMY_DATABASE_URI"]        = f"sqlite:///{DATABASE_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.secret_key = "flood_detection_secret_key_123"  # change for production
app.debug      = False   # set True only during development

db = SQLAlchemy(app)

# ── Thresholds (must match receiver.py) ──────────────────────────────────────
FLOOD_THRESHOLD   = 15.0   # cm — bucket demo
WARNING_THRESHOLD = 10.0   # cm — bucket demo

# Uncomment for real river deployment (comment out the lines above):
# FLOOD_THRESHOLD   = 800.0   # 8 m in cm
# WARNING_THRESHOLD = 680.0   # 6.8 m in cm

# ── SQLAlchemy Models ─────────────────────────────────────────────────────────
#
# These MUST match the CREATE TABLE statements in receiver.py exactly.
# Column names are the Python attribute names (SQLAlchemy maps them 1-to-1).

class WaterLevel(db.Model):
    __tablename__ = "water_level"
    id          = db.Column(db.Integer, primary_key=True)
    timestamp   = db.Column(db.DateTime, default=datetime.now)
    water_level = db.Column(db.Float, nullable=False)   # cm, filtered
    river_name  = db.Column(db.String(100), default="Gateway")


class FloodEvent(db.Model):
    __tablename__ = "flood_event"
    id              = db.Column(db.Integer, primary_key=True)
    start_time      = db.Column(db.DateTime, nullable=False)
    end_time        = db.Column(db.DateTime, nullable=True)
    max_water_level = db.Column(db.Float, nullable=False)  # 'max_water_level' — matches receiver.py
    river_name      = db.Column(db.String(100), default="Gateway")
    severity        = db.Column(db.String(20), default="Moderate")
    description     = db.Column(db.String(500))


class AlertLog(db.Model):
    __tablename__ = "alert_log"
    id          = db.Column(db.Integer, primary_key=True)
    timestamp   = db.Column(db.DateTime, default=datetime.now)
    alert_type  = db.Column(db.String(50))
    water_level = db.Column(db.Float)
    message     = db.Column(db.String(500))


# ── Helper Functions ──────────────────────────────────────────────────────────

def check_flood_status(water_level_cm: float) -> str:
    if water_level_cm >= FLOOD_THRESHOLD:
        return "FLOOD"
    if water_level_cm >= WARNING_THRESHOLD:
        return "WARNING"
    return "NORMAL"


def calculate_trend(readings: list) -> tuple:
    """Return (trend_text, rate_cm_per_hour) from the last 3 readings."""
    if len(readings) < 3:
        return "STABLE", 0.0
    recent     = readings[-3:]
    oldest_lvl = recent[0].water_level
    newest_lvl = recent[-1].water_level
    time_diff  = (recent[-1].timestamp - recent[0].timestamp).total_seconds() / 3600.0
    if time_diff < 0.001:
        return "STABLE", 0.0
    rate = (newest_lvl - oldest_lvl) / time_diff   # cm/hour
    if rate > 2:
        return "RISING FAST", rate
    if rate > 0.5:
        return "RISING", rate
    if rate < -2:
        return "FALLING FAST", rate
    if rate < -0.5:
        return "FALLING", rate
    return "STABLE", rate


def get_statistics(readings: list) -> dict:
    if not readings:
        return {"min": 0, "max": 0, "avg": 0, "current": 0}
    levels = [r.water_level for r in readings]
    return {
        "min":     round(min(levels), 1),
        "max":     round(max(levels), 1),
        "avg":     round(sum(levels) / len(levels), 1),
        "current": round(levels[-1], 1),
    }


def auto_alert(status: str, water_level: float) -> None:
    """Log an alert row — called within an active db.session."""
    alert = AlertLog(
        alert_type  = status,
        water_level = water_level,
        message     = f"Water level {water_level:.1f} cm — Status: {status}",
    )
    db.session.add(alert)


def get_active_status_duration() -> tuple:
    active = FloodEvent.query.filter(FloodEvent.end_time.is_(None)).first()
    if active:
        delta   = datetime.now() - active.start_time
        hours   = int(delta.total_seconds() / 3600)
        minutes = int((delta.total_seconds() % 3600) / 60)
        return active.severity, f"{hours}h {minutes}m"
    return None, None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    recent_time = datetime.now() - timedelta(hours=24)
    readings    = (WaterLevel.query
                   .filter(WaterLevel.timestamp >= recent_time)
                   .order_by(WaterLevel.timestamp)
                   .all())

    labels  = [r.timestamp.strftime("%H:%M") for r in readings]
    data_cm = [r.water_level for r in readings]

    latest_reading = (WaterLevel.query
                      .order_by(WaterLevel.timestamp.desc())
                      .first())

    trend_text, trend_rate = calculate_trend(readings)
    stats = get_statistics(readings)
    active_severity, active_duration = get_active_status_duration()

    current_status    = "NORMAL"
    status_text       = "Safe"
    status_description = "No active flood warnings"
    alert_message     = None
    latest_level_cm   = 0

    if latest_reading:
        latest_level_cm = round(latest_reading.water_level, 1)
        current_status  = check_flood_status(latest_reading.water_level)

        if current_status == "FLOOD":
            status_text        = "FLOOD"
            status_description = (f"Water level at {latest_level_cm} cm — "
                                  f"exceeds threshold of {FLOOD_THRESHOLD:.0f} cm!")
            alert_message      = f"FLOOD ALERT! Water level at {latest_level_cm} cm!"
        elif current_status == "WARNING":
            status_text        = "Warning"
            status_description = (f"Water level at {latest_level_cm} cm — "
                                  f"approaching flood threshold.")
            alert_message      = (f"WARNING: Water level at {latest_level_cm} cm "
                                  f"(threshold {FLOOD_THRESHOLD:.0f} cm).")

    recent_log_readings = (WaterLevel.query
                           .order_by(WaterLevel.timestamp.desc())
                           .limit(8)
                           .all())
    recent_floods = (FloodEvent.query
                     .order_by(FloodEvent.start_time.desc())
                     .limit(5)
                     .all())

    last_updated = (latest_reading.timestamp.strftime("%H:%M")
                    if latest_reading else "--:--")

    return render_template(
        "dashboard.html",
        labels            = labels,
        data_cm           = data_cm,
        latest_level_cm   = latest_level_cm,
        current_status    = current_status,
        status_text       = status_text,
        status_description= status_description,
        alert_message     = alert_message,
        threshold_cm      = int(FLOOD_THRESHOLD),
        warning_cm        = int(WARNING_THRESHOLD),
        recent_readings   = recent_log_readings,
        recent_floods     = recent_floods,
        last_updated      = last_updated,
        trend_text        = trend_text,
        trend_rate        = round(trend_rate, 1),
        stats             = stats,
        active_severity   = active_severity,
        active_duration   = active_duration,
    )


@app.route("/api/live-data")
def api_live_data():
    """JSON endpoint — polled by the dashboard chart every few seconds.

    Returns age_seconds so the frontend can tell the difference between
    "no new sensor reading yet" and "the sensor/gateway pipeline is dead" —
    instead of just reporting whether the fetch() itself succeeded.
    """
    latest = WaterLevel.query.order_by(WaterLevel.timestamp.desc()).first()
    if not latest:
        return jsonify({"error": "no data yet"}), 404
    age_seconds = (datetime.now() - latest.timestamp).total_seconds()
    return jsonify({
        "timestamp":   latest.timestamp.isoformat(),
        "water_level": latest.water_level,
        "river_name":  latest.river_name,
        "status":      check_flood_status(latest.water_level),
        "age_seconds": round(age_seconds, 1),
    })


@app.route("/flood-history")
def flood_history():
    floods = FloodEvent.query.order_by(FloodEvent.start_time.desc()).all()
    flood_by_year_month: dict = {}
    for flood in floods:
        year  = flood.start_time.year
        month = flood.start_time.strftime("%B")
        flood_by_year_month.setdefault(year, {}).setdefault(month, []).append(flood)

    total_events  = len(floods)
    active_events = sum(1 for f in floods if f.end_time is None)
    peak_level    = round(max((f.max_water_level for f in floods), default=0), 1)

    return render_template(
        "flood_history.html",
        flood_by_year_month = flood_by_year_month,
        total_events        = total_events,
        active_events       = active_events,
        peak_level          = peak_level,
        flood_threshold     = int(FLOOD_THRESHOLD),
    )


DEMO_MODE = os.getenv("DEMO_MODE", "0").lower() in {"1", "true", "yes", "on"}


@app.route("/simulate-sensor")
def simulate_sensor():
    """
    Demo/dev-only route: inject a synthetic reading into the DB so you can
    test the dashboard without real hardware running.

    Disabled by default. To use it while testing without hardware, start
    the app with:  DEMO_MODE=1 python3 app.py
    It is intentionally OFF for normal/demo-day runs so nobody can trigger
    a fake flood by accident (e.g. clicking the wrong link, or a stray
    request) while presenting.
    """
    if not DEMO_MODE:
        return jsonify({
            "error": "Simulation is disabled. This route only works when "
                     "the server is started with DEMO_MODE=1."
        }), 403

    base  = 8.0
    level = round(base + random.uniform(-3, 6), 1)
    if random.random() < 0.15:
        level = round(random.uniform(15, 18), 1)
    level = max(0.0, level)

    reading = WaterLevel(water_level=level, river_name="Simulation")
    db.session.add(reading)

    status = check_flood_status(level)

    if status == "FLOOD":
        active = FloodEvent.query.filter(FloodEvent.end_time.is_(None)).first()
        if not active:
            severity = ("Severe" if level >= FLOOD_THRESHOLD + 3
                        else "High" if level >= FLOOD_THRESHOLD + 1
                        else "Moderate")
            event = FloodEvent(
                start_time      = datetime.now(),
                max_water_level = level,
                river_name      = "Simulation",
                severity        = severity,
                description     = f"Simulated flood at {level} cm",
            )
            db.session.add(event)
            auto_alert("FLOOD", level)
            flash(f"FLOOD DETECTED! Severity: {severity}", "danger")
        else:
            if level > active.max_water_level:
                active.max_water_level = level
    elif status == "WARNING":
        auto_alert("WARNING", level)
    else:
        last_alert = AlertLog.query.order_by(AlertLog.timestamp.desc()).first()
        if last_alert and last_alert.alert_type in {"FLOOD", "WARNING"}:
            auto_alert("RECOVERY", level)
            active = FloodEvent.query.filter(FloodEvent.end_time.is_(None)).first()
            if active:
                active.end_time = datetime.now()

    db.session.commit()
    flash(f"Simulated reading: {level} cm — Status: {status}", "info")
    return redirect(url_for("dashboard"))


@app.route("/end-flood/<int:flood_id>")
def end_flood(flood_id: int):
    flood = FloodEvent.query.get(flood_id)
    if flood:
        flood.end_time = datetime.now()
        db.session.commit()
        flash("Flood event marked as ended", "info")
    return redirect(url_for("flood_history"))


# ── DB Initialisation ─────────────────────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()
        print(f"[DB] Tables ready at {DATABASE_PATH}")


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
