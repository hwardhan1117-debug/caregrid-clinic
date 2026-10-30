import csv
import io
import os
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, g, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)
DB_PATH = os.environ.get("DB_PATH", "clinic.db")
START_TIME = time.time()
IST = timezone(timedelta(hours=5, minutes=30))
FMT = "%Y-%m-%d %H:%M:%S"

DOCTORS = ["Dr. Sharma (General)", "Dr. Verma (Pediatrics)", "Dr. Khan (Dentist)"]
SLOTS = ["10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "14:00", "14:30", "15:00"]

# SLA targets (shown on the status page)
SLA_RESPONSE_MS = 2000
SLA_WAIT_MIN = 20

METRICS = {"requests": 0, "total_ms": 0.0, "errors": 0}

NEW_COLUMNS = {
    "status": "TEXT DEFAULT 'Booked'",
    "booked_at": "TEXT",
    "checkin_at": "TEXT",
    "consult_at": "TEXT",
    "checkout_at": "TEXT",
    "diagnosis": "TEXT",
    "prescription": "TEXT",
    "notes": "TEXT",
    "followup": "TEXT",
}


def now():
    return datetime.now(IST).strftime(FMT)


def minutes_between(a, b):
    if not a or not b:
        return None
    try:
        return round((datetime.strptime(b, FMT) - datetime.strptime(a, FMT)).total_seconds() / 60, 1)
    except ValueError:
        return None


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient TEXT NOT NULL,
                phone TEXT NOT NULL,
                doctor TEXT NOT NULL,
                date TEXT NOT NULL,
                slot TEXT NOT NULL,
                UNIQUE(doctor, date, slot)
            )"""
        )
        existing = [r["name"] for r in conn.execute("PRAGMA table_info(appointments)")]
        for col, ctype in NEW_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE appointments ADD COLUMN {col} {ctype}")
        conn.execute("UPDATE appointments SET status = 'Booked' WHERE status IS NULL")


# ---------- request metrics (for the Live Status page) ----------
@app.before_request
def _start_timer():
    g.t0 = time.time()


@app.after_request
def _record(resp):
    if request.endpoint not in ("api_metrics", "static"):
        METRICS["requests"] += 1
        METRICS["total_ms"] += (time.time() - g.t0) * 1000
        if resp.status_code >= 500:
            METRICS["errors"] += 1
    return resp


# ---------- main page and booking ----------
@app.route("/")
def index():
    msg = request.args.get("msg")
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM appointments ORDER BY date, slot").fetchall()
    return render_template("index.html", doctors=DOCTORS, slots=SLOTS, rows=rows, msg=msg)


@app.route("/book", methods=["POST"])
def book():
    patient = request.form["patient"].strip()
    phone = request.form["phone"].strip()
    doctor = request.form["doctor"]
    date = request.form["date"]
    slot = request.form["slot"]
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO appointments (patient, phone, doctor, date, slot, status, booked_at) "
                "VALUES (?,?,?,?,?,'Booked',?)",
                (patient, phone, doctor, date, slot, now()),
            )
        msg = "Appointment booked successfully!"
    except sqlite3.IntegrityError:
        msg = "Sorry, this slot is already booked. Please choose another."
    return redirect(url_for("index", msg=msg))


@app.route("/cancel/<int:appt_id>", methods=["POST"])
def cancel(appt_id):
    with get_db() as conn:
        conn.execute("DELETE FROM appointments WHERE id = ?", (appt_id,))
    return redirect(url_for("index", msg="Appointment cancelled."))


# ---------- patient visit lifecycle ----------
@app.route("/checkin/<int:appt_id>", methods=["POST"])
def checkin(appt_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE appointments SET status='Checked-in', checkin_at=? WHERE id=? AND status='Booked'",
            (now(), appt_id),
        )
    return redirect(url_for("index", msg="Patient checked in."))


@app.route("/consult/<int:appt_id>", methods=["POST"])
def consult(appt_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE appointments SET status='In Consultation', consult_at=? WHERE id=? AND status='Checked-in'",
            (now(), appt_id),
        )
    return redirect(url_for("index", msg="Consultation started."))


@app.route("/complete/<int:appt_id>", methods=["POST"])
def complete(appt_id):
    f = request.form
    with get_db() as conn:
        conn.execute(
            "UPDATE appointments SET status='Completed', checkout_at=?, diagnosis=?, prescription=?, "
            "notes=?, followup=? WHERE id=? AND status='In Consultation'",
            (now(), f.get("diagnosis", "").strip(), f.get("prescription", "").strip(),
             f.get("notes", "").strip(), f.get("followup", "").strip(), appt_id),
        )
    return redirect(url_for("report", appt_id=appt_id))


@app.route("/report/<int:appt_id>")
def report(appt_id):
    with get_db() as conn:
        r = conn.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()
    if r is None:
        return redirect(url_for("index", msg="Appointment not found."))
    wait = minutes_between(r["checkin_at"], r["consult_at"])
    total = minutes_between(r["checkin_at"], r["checkout_at"])
    return render_template("report.html", r=r, wait=wait, total=total)


# ---------- live status and health ----------
def metrics_snapshot():
    n = METRICS["requests"]
    avg_ms = round(METRICS["total_ms"] / n, 1) if n else 0.0
    return {
        "uptime_seconds": int(time.time() - START_TIME),
        "started_at": datetime.fromtimestamp(START_TIME, IST).strftime(FMT),
        "requests": n,
        "errors": METRICS["errors"],
        "avg_response_ms": avg_ms,
        "sla_response_ms": SLA_RESPONSE_MS,
        "sla_met": avg_ms <= SLA_RESPONSE_MS and METRICS["errors"] == 0,
    }


@app.route("/health")
def health():
    try:
        with get_db() as conn:
            conn.execute("SELECT 1")
        db_status = "ok"
    except Exception:
        db_status = "down"
    code = 200 if db_status == "ok" else 500
    return jsonify(status="ok" if code == 200 else "error", db=db_status,
                   uptime_seconds=int(time.time() - START_TIME)), code


@app.route("/status")
def status():
    return render_template("status.html", m=metrics_snapshot())


@app.route("/api/metrics")
def api_metrics():
    return jsonify(metrics_snapshot())


# ---------- admin dashboard and CSV export ----------
@app.route("/admin")
def admin():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM appointments").fetchall()
    today = datetime.now(IST).strftime("%Y-%m-%d")
    by_doctor = Counter(r["doctor"] for r in rows)
    by_slot = Counter(r["slot"] for r in rows)
    by_status = Counter(r["status"] for r in rows)
    waits = [w for w in (minutes_between(r["checkin_at"], r["consult_at"]) for r in rows) if w is not None]
    visits = [v for v in (minutes_between(r["checkin_at"], r["checkout_at"]) for r in rows) if v is not None]
    stats = {
        "total": len(rows),
        "today": sum(1 for r in rows if r["date"] == today),
        "completed": by_status.get("Completed", 0),
        "avg_wait": round(sum(waits) / len(waits), 1) if waits else None,
        "avg_visit": round(sum(visits) / len(visits), 1) if visits else None,
        "busiest_slot": by_slot.most_common(1)[0][0] if by_slot else "-",
        "sla_wait_min": SLA_WAIT_MIN,
        "by_doctor": [(d, by_doctor.get(d, 0)) for d in DOCTORS],
        "by_status": list(by_status.items()),
    }
    return render_template("admin.html", s=stats)


@app.route("/export.csv")
def export_csv():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM appointments ORDER BY date, slot").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "patient", "phone", "doctor", "date", "slot", "status", "booked_at", "checkin_at",
                "consult_at", "checkout_at", "wait_min", "visit_min", "diagnosis", "prescription", "notes", "followup"])
    for r in rows:
        w.writerow([r["id"], r["patient"], r["phone"], r["doctor"], r["date"], r["slot"], r["status"],
                    r["booked_at"], r["checkin_at"], r["consult_at"], r["checkout_at"],
                    minutes_between(r["checkin_at"], r["consult_at"]),
                    minutes_between(r["checkin_at"], r["checkout_at"]),
                    r["diagnosis"], r["prescription"], r["notes"], r["followup"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=appointments.csv"})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)