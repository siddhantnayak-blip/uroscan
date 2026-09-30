"""UroScan - Smart Urine Strip Reader with a Healthcare Database.

Flask web app: login/sign-up with roles, strip scanning (OpenCV + KNN colour matching),
MySQL storage with triggers/views/procedures/transactions, dashboards with trends, AI summary + chat.
"""
import os
import io
import re
import json
import random
import secrets
import logging
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for, session, flash,
                   jsonify, abort, send_file, g)
from werkzeug.security import generate_password_hash, check_password_hash

import db as database
from db import get_db, query, execute, audit, close_db, init_db, DB_FEATURES
from reference import CHART, PAD_ORDER, ANALYTE_INFO
import vision
import ai
from trends import analyse_trend

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("uroscan")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
app.teardown_appcontext(close_db)

DB_STATUS = {"ok": False, "error": None}
try:
    init_db()
    DB_STATUS["ok"] = True
except Exception as e:  # app still starts and shows a friendly page
    DB_STATUS["error"] = str(e)
    log.error("Database init failed: %s", e)

ROLE_LABELS = {"patient": "Patient", "labtech": "Lab technician", "clinician": "Clinician", "admin": "Admin"}
STATUS_ORDER = {"Normal": 0, "Trace": 1, "Low": 2, "High": 2}


# ================================================================ helpers
@app.before_request
def load_user():
    g.user = None
    if not DB_STATUS["ok"] and request.endpoint not in ("static", "health"):
        try:
            init_db(); DB_STATUS.update(ok=True, error=None)
        except Exception as e:
            DB_STATUS["error"] = str(e)
            return render_template("error.html", title="Database not reachable",
                                   message="UroScan can't reach its MySQL database right now. If you're the admin, "
                                           "check the DB_* settings and that the Aiven service is running."), 503
    uid = session.get("uid")
    if uid:
        g.user = query("SELECT user_id, full_name, email, role, is_active FROM users WHERE user_id=%s", (uid,), one=True)
        if not g.user or not g.user["is_active"]:
            session.clear(); g.user = None
    if request.method == "POST" and request.endpoint not in ("api_chat",):
        if request.form.get("csrf_token") != session.get("csrf_token"):
            abort(400, "Form expired. Please go back, refresh and try again.")


@app.context_processor
def inject():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    unread = 0
    if g.get("user") and g.user["role"] == "patient":
        row = query("SELECT COUNT(*) AS n FROM alert WHERE patient_id=%s AND is_read=0", (g.user["user_id"],), one=True)
        unread = row["n"] if row else 0
    return dict(csrf_token=session["csrf_token"], user=g.get("user"), ROLE_LABELS=ROLE_LABELS,
                unread_alerts=unread, ai_on=ai.ai_available(), now=datetime.now())


def login_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not g.user:
                flash("Please log in first.", "warn")
                return redirect(url_for("login", next=request.path))
            if roles and g.user["role"] not in roles:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def valid_password(pw):
    return len(pw) >= 8 and re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw)


def python_status(analyte, label, value):
    """Same rule as trigger trg_result_status (used only if triggers aren't allowed on the server)."""
    _, mn, mx, _ = ANALYTE_INFO[analyte]
    if mn <= value <= mx:
        return "Normal"
    if label.startswith("Trace"):
        return "Trace"
    return "Low" if value < mn else "High"


def analyte_ids():
    return {r["name"]: r["analyte_id"] for r in query("SELECT analyte_id, name FROM analyte")}


def can_view_patient(pid):
    u = g.user
    return u and (u["role"] in ("clinician", "labtech", "admin") or u["user_id"] == pid)


# ================================================================ core transaction: save one scan
def save_scan(patient_id, created_by, readings, image=None, annotated=None, quality=None,
              is_demo=False, when=None, summarise=True):
    """Stores sample + strip + report + 10 results as ONE ACID transaction."""
    conn = get_db()
    ids = analyte_ids()
    when = when or datetime.now()
    cur = conn.cursor()
    try:
        conn.begin()                                                         # START TRANSACTION
        cur.execute("INSERT INTO urine_sample (patient_id, collected_by, collected_at) VALUES (%s,%s,%s)",
                    (patient_id, created_by, when))
        sample_id = cur.lastrowid
        cur.execute("INSERT INTO test_strip (sample_id, image, annotated, quality_json) VALUES (%s,%s,%s,%s)",
                    (sample_id, image, annotated, json.dumps(quality or {})))
        cur.execute("INSERT INTO test_report (sample_id, lab_id, created_by, created_at, is_demo) "
                    "VALUES (%s,(SELECT MIN(lab_id) FROM laboratory),%s,%s,%s)",
                    (sample_id, created_by, when, int(is_demo)))
        report_id = cur.lastrowid
        cur.execute("SAVEPOINT before_results")
        for r in readings:
            rgb = r.get("rgb") or [None, None, None]
            cur.execute("INSERT INTO diagnostic_result (report_id, analyte_id, level_index, level_label, numeric_value, "
                        "approx_value, confidence, delta_e, r, g, b, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (report_id, ids[r["analyte"]], r["level_index"], r["level_label"], r["numeric_value"],
                         r.get("approx_value"), r.get("confidence", 1), r.get("delta_e"), rgb[0], rgb[1], rgb[2],
                         python_status(r["analyte"], r["level_label"], r["numeric_value"])))
        if not DB_FEATURES["triggers"]:
            # the server refused triggers -> do the same work in Python inside the same transaction
            cur.execute("SELECT d.status, d.level_label, d.analyte_id, a.name FROM diagnostic_result d "
                        "JOIN analyte a ON a.analyte_id=d.analyte_id WHERE report_id=%s", (report_id,))
            rows = cur.fetchall()
            cur.execute("UPDATE test_report SET abnormal_count=%s WHERE report_id=%s",
                        (sum(r["status"] != "Normal" for r in rows), report_id))
            for r in rows:
                if r["status"] in ("High", "Low"):
                    cur.execute("INSERT INTO alert (patient_id, report_id, analyte_id, message) VALUES (%s,%s,%s,%s)",
                                (patient_id, report_id, r["analyte_id"], f"{r['name']} is {r['status']} ({r['level_label']})"))
            cur.execute("INSERT INTO audit_log (user_id, action, details) VALUES (%s,'REPORT_CREATED',%s)",
                        (created_by, f"report_id={report_id}"))
        conn.commit()                                                        # COMMIT - saved for good
    except Exception:
        conn.rollback()                                                      # ROLLBACK - nothing half-saved
        cur.close()
        raise
    cur.close()

    # results as stored (status set by the trigger)
    stored = query("SELECT a.name AS analyte, d.level_label, d.status FROM diagnostic_result d "
                   "JOIN analyte a ON a.analyte_id=d.analyte_id WHERE d.report_id=%s ORDER BY a.pad_order", (report_id,))
    if summarise:
        text, _src = ai.summarise(stored)
    else:
        text = ai.rule_summary(stored)
    execute("UPDATE test_report SET ai_summary=%s WHERE report_id=%s", (text, report_id))
    streak_alerts(patient_id, report_id)
    return report_id


def streak_alerts(patient_id, report_id):
    """Alert if the same test is abnormal in 3 scans in a row."""
    hist = patient_history(patient_id)
    for name in PAD_ORDER:
        t = analyse_trend(hist.get(name, []))
        if t["streak"] == 3:
            execute("INSERT INTO alert (patient_id, report_id, message) VALUES (%s,%s,%s)",
                    (patient_id, report_id, f"{name} has been abnormal in 3 scans in a row - please see a doctor."))


def patient_history(pid):
    rows = query("SELECT r.report_id, r.created_at, a.name, d.level_index, d.level_label, d.numeric_value, d.status "
                 "FROM test_report r JOIN urine_sample s ON s.sample_id=r.sample_id "
                 "JOIN diagnostic_result d ON d.report_id=r.report_id JOIN analyte a ON a.analyte_id=d.analyte_id "
                 "WHERE s.patient_id=%s ORDER BY r.created_at, r.report_id", (pid,))
    hist = {}
    for r in rows:
        hist.setdefault(r["name"], []).append(r)
    return hist


# ================================================================ public pages
@app.route("/")
def index():
    if g.user:
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/health")
def health():
    try:
        query("SELECT 1 AS ok")
        return "ok", 200
    except Exception:
        return "db down", 503


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("dashboard"))
    form = request.form
    if request.method == "POST":
        name, email = form.get("full_name", "").strip(), form.get("email", "").strip().lower()
        pw, pw2, role = form.get("password", ""), form.get("confirm", ""), form.get("role", "patient")
        errors = []
        if len(name) < 2: errors.append("Please enter your full name.")
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email): errors.append("Please enter a valid email.")
        if not valid_password(pw): errors.append("Password must be at least 8 characters with a letter and a number.")
        if pw != pw2: errors.append("Passwords don't match.")
        if role not in ("patient", "labtech", "clinician"): errors.append("Choose a valid role.")
        if not errors and query("SELECT 1 FROM users WHERE email=%s", (email,), one=True):
            errors.append("An account with this email already exists. Try logging in.")
        if errors:
            for e in errors: flash(e, "error")
            return render_template("register.html", form=form)
        if email == os.environ.get("ADMIN_EMAIL", "").strip().lower():
            role = "admin"
        conn = get_db(); cur = conn.cursor()
        try:
            conn.begin()
            cur.execute("INSERT INTO users (full_name, email, password_hash, role) VALUES (%s,%s,%s,%s)",
                        (name, email, generate_password_hash(pw), role))
            uid = cur.lastrowid
            if role == "patient":
                dob = form.get("dob") or None
                gender = form.get("gender") if form.get("gender") in ("Male", "Female", "Other") else None
                cur.execute("INSERT INTO patient (patient_id, dob, gender) VALUES (%s,%s,%s)", (uid, dob, gender))
            elif role == "clinician":
                cur.execute("INSERT INTO clinician (clinician_id, specialty, lab_id) VALUES (%s,%s,(SELECT MIN(lab_id) FROM laboratory))",
                            (uid, form.get("specialty") or None))
            elif role == "labtech":
                cur.execute("INSERT INTO lab_technician (labtech_id, lab_id) VALUES (%s,(SELECT MIN(lab_id) FROM laboratory))", (uid,))
            conn.commit()
        except Exception as e:
            conn.rollback(); log.exception("register failed")
            flash("Couldn't create the account. Please try again.", "error")
            return render_template("register.html", form=form)
        finally:
            cur.close()
        audit(uid, "REGISTER", role)
        session.clear(); session["uid"] = uid
        flash(f"Welcome to UroScan, {name.split()[0]}!", "ok")
        return redirect(url_for("dashboard"))
    return render_template("register.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")[:45]
        fails = query("SELECT COUNT(*) AS n FROM login_attempt WHERE email=%s AND success=0 "
                      "AND attempted_at > NOW() - INTERVAL 15 MINUTE", (email,), one=True)["n"]
        if fails >= 5:
            flash("Too many failed attempts. Please wait 15 minutes or reset your password.", "error")
            return render_template("login.html", email=email)
        u = query("SELECT * FROM users WHERE email=%s", (email,), one=True)
        ok = bool(u and u["is_active"] and check_password_hash(u["password_hash"], pw))
        execute("INSERT INTO login_attempt (email, success, ip) VALUES (%s,%s,%s)", (email, int(ok), ip))
        if not ok:
            flash("Wrong email or password.", "error")
            return render_template("login.html", email=email)
        session.clear()
        session["uid"] = u["user_id"]
        session.permanent = bool(request.form.get("remember"))
        audit(u["user_id"], "LOGIN")
        nxt = request.args.get("next", "")
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
    return render_template("login.html", email="")


@app.route("/logout")
def logout():
    if g.user:
        audit(g.user["user_id"], "LOGOUT")
    session.clear()
    flash("You've been logged out.", "ok")
    return redirect(url_for("login"))


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    link = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        u = query("SELECT user_id FROM users WHERE email=%s", (email,), one=True)
        if u:
            token = secrets.token_hex(32)
            execute("INSERT INTO password_reset (token, user_id, expires_at) VALUES (%s,%s,%s)",
                    (token, u["user_id"], datetime.now() + timedelta(minutes=30)))
            audit(u["user_id"], "RESET_REQUESTED")
            link = url_for("reset", token=token, _external=True)
            log.info("Password reset link for %s: %s", email, link)
        flash("If that email is registered, a reset link has been created (valid for 30 minutes).", "ok")
        if os.environ.get("SHOW_RESET_LINK", "1") != "1":
            link = None
    return render_template("forgot.html", link=link)


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    row = query("SELECT * FROM password_reset WHERE token=%s AND used=0 AND expires_at > NOW()", (token,), one=True)
    if not row:
        flash("This reset link is invalid or has expired.", "error")
        return redirect(url_for("forgot"))
    if request.method == "POST":
        pw, pw2 = request.form.get("password", ""), request.form.get("confirm", "")
        if not valid_password(pw) or pw != pw2:
            flash("Passwords must match and have 8+ characters with a letter and a number.", "error")
            return render_template("reset.html")
        execute("UPDATE users SET password_hash=%s WHERE user_id=%s", (generate_password_hash(pw), row["user_id"]), commit=False)
        execute("UPDATE password_reset SET used=1 WHERE token=%s", (token,))
        audit(row["user_id"], "PASSWORD_RESET")
        flash("Password updated. Please log in.", "ok")
        return redirect(url_for("login"))
    return render_template("reset.html")


# ================================================================ dashboard
@app.route("/dashboard")
@login_required()
def dashboard():
    role = g.user["role"]
    if role == "clinician":
        return redirect(url_for("clinician"))
    if role == "labtech":
        return redirect(url_for("scan"))
    if role == "admin":
        return redirect(url_for("admin"))
    return render_patient_dashboard(g.user["user_id"], g.user["full_name"])


def render_patient_dashboard(pid, name):
    latest = query("SELECT r.report_id, r.created_at, r.ai_summary, r.abnormal_count FROM test_report r "
                   "JOIN urine_sample s ON s.sample_id=r.sample_id WHERE s.patient_id=%s "
                   "ORDER BY r.created_at DESC, r.report_id DESC LIMIT 1", (pid,), one=True)
    results = []
    if latest:
        results = query("SELECT a.name AS analyte, a.unit, d.level_label, d.status, d.confidence, d.r, d.g, d.b "
                        "FROM diagnostic_result d JOIN analyte a ON a.analyte_id=d.analyte_id "
                        "WHERE d.report_id=%s ORDER BY a.pad_order", (latest["report_id"],))
    hist = patient_history(pid)
    trends = {}
    for n in PAD_ORDER:
        levels = CHART[n]
        mx_idx = max(i for i, l in enumerate(levels) if ANALYTE_INFO[n][1] <= l[1] <= ANALYTE_INFO[n][2])
        trends[n] = analyse_trend(hist.get(n, []), mx_idx)
    n_scans = query("SELECT COUNT(*) AS n FROM test_report r JOIN urine_sample s ON s.sample_id=r.sample_id "
                    "WHERE s.patient_id=%s", (pid,), one=True)["n"]
    alerts = query("SELECT * FROM alert WHERE patient_id=%s ORDER BY created_at DESC, alert_id DESC LIMIT 8", (pid,))
    notes = query("SELECT n.note, n.created_at, u.full_name, n.report_id FROM clinician_note n "
                  "JOIN users u ON u.user_id=n.clinician_id JOIN test_report r ON r.report_id=n.report_id "
                  "JOIN urine_sample s ON s.sample_id=r.sample_id WHERE s.patient_id=%s ORDER BY n.created_at DESC LIMIT 3", (pid,))
    score = sum(r["status"] == "Normal" for r in results)
    return render_template("dashboard.html", pid=pid, patient_name=name, latest=latest, results=results,
                           trends=trends, n_scans=n_scans, alerts=alerts, score=score, notes=notes,
                           analytes=PAD_ORDER, info=ANALYTE_INFO)


@app.route("/api/patient/<int:pid>/chart-data")
@login_required()
def chart_data(pid):
    if not can_view_patient(pid):
        abort(403)
    hist = patient_history(pid)
    out = {"analytes": {}, "scans": []}
    for n in PAD_ORDER:
        levels = CHART[n]
        normal_idx = [i for i, l in enumerate(levels) if ANALYTE_INFO[n][1] <= l[1] <= ANALYTE_INFO[n][2]]
        out["analytes"][n] = {
            "labels": [l[0] for l in levels],
            "normal": [min(normal_idx), max(normal_idx)],
            "points": [{"t": r["created_at"].strftime("%d %b"), "y": r["level_index"], "label": r["level_label"],
                        "status": r["status"], "id": r["report_id"]} for r in hist.get(n, [])],
        }
    reps = query("SELECT r.report_id, r.created_at, "
                 "SUM(d.status='Normal') AS normal, SUM(d.status='Trace') AS trace, SUM(d.status IN ('High','Low')) AS abnormal "
                 "FROM test_report r JOIN urine_sample s ON s.sample_id=r.sample_id "
                 "JOIN diagnostic_result d ON d.report_id=r.report_id WHERE s.patient_id=%s "
                 "GROUP BY r.report_id, r.created_at ORDER BY r.created_at, r.report_id", (pid,))
    out["scans"] = [{"t": r["created_at"].strftime("%d %b"), "normal": int(r["normal"]), "trace": int(r["trace"]),
                     "abnormal": int(r["abnormal"])} for r in reps]
    counts = query("SELECT a.name, SUM(d.status<>'Normal') AS n FROM diagnostic_result d "
                   "JOIN analyte a ON a.analyte_id=d.analyte_id JOIN test_report r ON r.report_id=d.report_id "
                   "JOIN urine_sample s ON s.sample_id=r.sample_id WHERE s.patient_id=%s GROUP BY a.name, a.pad_order "
                   "ORDER BY a.pad_order", (pid,))
    out["abnormal_by_test"] = {c["name"]: int(c["n"] or 0) for c in counts}
    # radar: latest vs average, as % of each test's scale
    radar_latest, radar_avg = [], []
    for n in PAD_ORDER:
        pts = hist.get(n, [])
        top = len(CHART[n]) - 1
        radar_latest.append(round(100 * pts[-1]["level_index"] / top) if pts else 0)
        radar_avg.append(round(100 * sum(p["level_index"] for p in pts) / len(pts) / top) if pts else 0)
    out["radar"] = {"labels": PAD_ORDER, "latest": radar_latest, "average": radar_avg}
    return jsonify(out)


@app.route("/alerts/read", methods=["POST"])
@login_required("patient")
def alerts_read():
    execute("UPDATE alert SET is_read=1 WHERE patient_id=%s", (g.user["user_id"],))
    return redirect(url_for("dashboard"))


@app.route("/demo/load", methods=["POST"])
@login_required("patient")
def demo_load():
    """Adds 12 simulated past scans so the trend charts have data (clearly marked as demo)."""
    pid = g.user["user_id"]
    rng = random.Random(pid)
    start = datetime.now() - timedelta(days=66)
    for k in range(12):
        when = start + timedelta(days=5 * k + rng.randint(0, 2), hours=rng.randint(8, 11))
        lv = {n: 0 for n in PAD_ORDER}
        lv["Specific Gravity"] = rng.choice([2, 3, 3, 4])
        lv["pH"] = rng.choice([1, 1, 2, 3])
        lv["Urobilinogen"] = rng.choice([0, 0, 1])
        lv["Glucose"] = [0, 0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 3][k]            # slowly rising glucose
        if 5 <= k <= 7:                                                      # an infection episode
            lv["Leukocytes"] = [2, 3, 1][k - 5]
            lv["Nitrite"] = 1 if k in (5, 6) else 0
            lv["Blood"] = 1 if k == 6 else 0
        if k in (9, 10):
            lv["Protein"] = 1
        readings = [dict(analyte=n, level_index=i, level_label=CHART[n][i][0], numeric_value=CHART[n][i][1],
                         approx_value=CHART[n][i][1], confidence=1, rgb=list(CHART[n][i][2]))
                    for n, i in lv.items()]
        save_scan(pid, pid, readings, is_demo=True, when=when, summarise=False)
    audit(pid, "DEMO_DATA", "12 demo scans")
    flash("Added 12 demo scans (marked 'demo') so you can explore the charts.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/demo/clear", methods=["POST"])
@login_required("patient")
def demo_clear():
    ids = [r["sample_id"] for r in query("SELECT s.sample_id FROM urine_sample s JOIN test_report r "
                                         "ON r.sample_id=s.sample_id WHERE s.patient_id=%s AND r.is_demo=1",
                                         (g.user["user_id"],))]
    if ids:
        execute("DELETE FROM urine_sample WHERE sample_id IN (" + ",".join(["%s"] * len(ids)) + ")", tuple(ids))
    flash("Demo scans removed.", "ok")
    return redirect(url_for("dashboard"))


# ================================================================ scanning
@app.route("/scan", methods=["GET", "POST"])
@login_required("patient", "labtech", "clinician")
def scan():
    patients = []
    if g.user["role"] != "patient":
        patients = query("SELECT u.user_id, u.full_name, u.email FROM users u JOIN patient p ON p.patient_id=u.user_id "
                         "WHERE u.is_active=1 ORDER BY u.full_name")
    if request.method == "POST":
        pid = g.user["user_id"] if g.user["role"] == "patient" else int(request.form.get("patient_id") or 0)
        if g.user["role"] != "patient" and not any(p["user_id"] == pid for p in patients):
            flash("Please choose a patient.", "error")
            return render_template("scan.html", patients=patients)
        sample = request.form.get("sample")
        if sample and re.fullmatch(r"[1-6]", sample):
            data = open(os.path.join(app.static_folder, "samples", f"sample_{sample}.jpg"), "rb").read()
        else:
            f = request.files.get("photo")
            if not f or not f.filename:
                flash("Please choose or take a photo of the strip.", "error")
                return render_template("scan.html", patients=patients)
            data = f.read()
        try:
            out = vision.analyse(data)
        except vision.ScanError as e:
            flash(str(e), "error")
            return render_template("scan.html", patients=patients)
        except Exception:
            log.exception("scan failed")
            flash("Something went wrong reading that photo. Please try another one.", "error")
            return render_template("scan.html", patients=patients)
        try:
            rid = save_scan(pid, g.user["user_id"], out["results"], out["image"], out["annotated"], out["quality"])
        except Exception:
            log.exception("save failed")
            flash("Couldn't save the scan (the transaction was rolled back, nothing half-saved). Please retry.", "error")
            return render_template("scan.html", patients=patients)
        for w in out["quality"]["warnings"]:
            flash(w, "warn")
        return redirect(url_for("report", rid=rid))
    return render_template("scan.html", patients=patients)


def load_report(rid):
    rep = query("SELECT r.*, s.patient_id, s.collected_at, u.full_name AS patient_name, cb.full_name AS created_by_name, "
                "rv.full_name AS reviewer, t.quality_json, (t.image IS NOT NULL) AS has_image "
                "FROM test_report r JOIN urine_sample s ON s.sample_id=r.sample_id "
                "JOIN users u ON u.user_id=s.patient_id LEFT JOIN users cb ON cb.user_id=r.created_by "
                "LEFT JOIN users rv ON rv.user_id=r.reviewed_by LEFT JOIN test_strip t ON t.sample_id=s.sample_id "
                "WHERE r.report_id=%s", (rid,), one=True)
    if not rep or not can_view_patient(rep["patient_id"]):
        abort(404)
    return rep


@app.route("/report/<int:rid>")
@login_required()
def report(rid):
    rep = load_report(rid)
    results = query("SELECT a.name AS analyte, a.unit, a.description, d.* FROM diagnostic_result d "
                    "JOIN analyte a ON a.analyte_id=d.analyte_id WHERE d.report_id=%s ORDER BY a.pad_order", (rid,))
    for r in results:
        r["chart"] = CHART[r["analyte"]]
    notes = query("SELECT n.*, u.full_name FROM clinician_note n JOIN users u ON u.user_id=n.clinician_id "
                  "WHERE n.report_id=%s ORDER BY n.created_at", (rid,))
    quality = json.loads(rep["quality_json"]) if rep.get("quality_json") else {}
    prev = query("SELECT r.report_id FROM test_report r JOIN urine_sample s ON s.sample_id=r.sample_id "
                 "WHERE s.patient_id=%s AND r.report_id<%s ORDER BY r.report_id DESC LIMIT 1",
                 (rep["patient_id"], rid), one=True)
    return render_template("report.html", rep=rep, results=results, notes=notes, quality=quality, prev=prev)


@app.route("/report/<int:rid>/image/<kind>")
@login_required()
def report_image(rid, kind):
    rep = load_report(rid)
    col = "annotated" if kind == "annotated" else "image"
    row = query(f"SELECT {col} AS img FROM test_strip WHERE sample_id=%s", (rep["sample_id"],), one=True)
    if not row or not row["img"]:
        abort(404)
    return send_file(io.BytesIO(row["img"]), mimetype="image/jpeg")


@app.route("/report/<int:rid>/note", methods=["POST"])
@login_required("clinician")
def add_note(rid):
    load_report(rid)
    note = request.form.get("note", "").strip()
    if note:
        execute("INSERT INTO clinician_note (report_id, clinician_id, note) VALUES (%s,%s,%s)", (rid, g.user["user_id"], note[:2000]))
        audit(g.user["user_id"], "NOTE_ADDED", f"report_id={rid}")
    if request.form.get("review"):
        execute("UPDATE test_report SET reviewed_by=%s, reviewed_at=NOW() WHERE report_id=%s", (g.user["user_id"], rid))
        audit(g.user["user_id"], "REPORT_REVIEWED", f"report_id={rid}")
    flash("Saved.", "ok")
    return redirect(url_for("report", rid=rid))


@app.route("/report/<int:rid>/delete", methods=["POST"])
@login_required()
def delete_report(rid):
    rep = load_report(rid)
    if g.user["role"] not in ("admin",) and g.user["user_id"] != rep["patient_id"]:
        abort(403)
    execute("DELETE FROM urine_sample WHERE sample_id=%s", (rep["sample_id"],))   # cascades to strip/report/results
    audit(g.user["user_id"], "REPORT_DELETED", f"report_id={rid}")
    flash("Report deleted.", "ok")
    return redirect(url_for("history"))


@app.route("/history")
@login_required()
def history():
    pid = request.args.get("patient", type=int) if g.user["role"] != "patient" else g.user["user_id"]
    if not pid or not can_view_patient(pid):
        return redirect(url_for("dashboard"))
    where, args = ["s.patient_id=%s"], [pid]
    if request.args.get("abnormal"):
        where.append("r.abnormal_count > 0")
    if request.args.get("from"):
        where.append("r.created_at >= %s"); args.append(request.args["from"])
    if request.args.get("to"):
        where.append("r.created_at < %s + INTERVAL 1 DAY"); args.append(request.args["to"])
    an = request.args.get("analyte")
    if an in PAD_ORDER:
        where.append("EXISTS (SELECT 1 FROM diagnostic_result d JOIN analyte a ON a.analyte_id=d.analyte_id "
                     "WHERE d.report_id=r.report_id AND a.name=%s AND d.status<>'Normal')"); args.append(an)
    page = max(request.args.get("page", 1, type=int), 1)
    rows = query("SELECT r.report_id, r.created_at, r.abnormal_count, r.is_demo, r.reviewed_by, "
                 "(SELECT GROUP_CONCAT(a.name ORDER BY a.pad_order SEPARATOR ', ') FROM diagnostic_result d "
                 " JOIN analyte a ON a.analyte_id=d.analyte_id WHERE d.report_id=r.report_id AND d.status<>'Normal') AS flags "
                 "FROM test_report r JOIN urine_sample s ON s.sample_id=r.sample_id WHERE " + " AND ".join(where) +
                 " ORDER BY r.created_at DESC, r.report_id DESC LIMIT 16 OFFSET %s", tuple(args + [(page - 1) * 15]))
    has_next = len(rows) > 15
    pname = query("SELECT full_name FROM users WHERE user_id=%s", (pid,), one=True)["full_name"]
    return render_template("history.html", rows=rows[:15], page=page, has_next=has_next, analytes=PAD_ORDER,
                           args=request.args, pid=pid, pname=pname)


# ================================================================ AI chat
@app.route("/api/chat", methods=["POST"])
@login_required("patient", "clinician")
def api_chat():
    data = request.get_json(silent=True) or {}
    if data.get("csrf_token") != session.get("csrf_token"):
        return jsonify(error="expired"), 400
    q = (data.get("message") or "").strip()[:500]
    if not q:
        return jsonify(answer="Ask me something about your results.")
    pid = g.user["user_id"] if g.user["role"] == "patient" else int(data.get("patient_id") or 0)
    if not can_view_patient(pid):
        abort(403)
    hist = patient_history(pid)
    rows = sorted([(r["created_at"].strftime("%Y-%m-%d"), n, r["level_label"], r["status"])
                   for n, pts in hist.items() for r in pts])
    latest = query("SELECT a.name AS analyte, d.level_label, d.status FROM v_latest_results d "
                   "JOIN analyte a ON a.name=d.analyte WHERE d.patient_id=%s ORDER BY a.pad_order", (pid,)) \
        if DB_FEATURES["views"] else []
    return jsonify(answer=ai.chat(q, rows, latest))


# ================================================================ clinician
@app.route("/clinician")
@login_required("clinician", "admin")
def clinician():
    rows = query("SELECT u.user_id, u.full_name, u.email, COUNT(r.report_id) AS scans, MAX(r.created_at) AS last_scan, "
                 "(SELECT r2.abnormal_count FROM test_report r2 JOIN urine_sample s2 ON s2.sample_id=r2.sample_id "
                 "  WHERE s2.patient_id=u.user_id ORDER BY r2.created_at DESC, r2.report_id DESC LIMIT 1) AS latest_abnormal, "
                 "SUM(r.reviewed_by IS NULL AND r.abnormal_count>0) AS to_review "
                 "FROM users u JOIN patient p ON p.patient_id=u.user_id "
                 "LEFT JOIN urine_sample s ON s.patient_id=u.user_id LEFT JOIN test_report r ON r.sample_id=s.sample_id "
                 "GROUP BY u.user_id, u.full_name, u.email ORDER BY to_review DESC, latest_abnormal DESC, last_scan DESC")
    return render_template("clinician.html", rows=rows)


@app.route("/clinician/patient/<int:pid>")
@login_required("clinician", "admin", "labtech")
def clinician_patient(pid):
    u = query("SELECT full_name FROM users WHERE user_id=%s", (pid,), one=True)
    if not u:
        abort(404)
    return render_patient_dashboard(pid, u["full_name"])


# ================================================================ profile
@app.route("/profile", methods=["GET", "POST"])
@login_required()
def profile():
    if request.method == "POST":
        u = query("SELECT password_hash FROM users WHERE user_id=%s", (g.user["user_id"],), one=True)
        cur, new, new2 = request.form.get("current", ""), request.form.get("password", ""), request.form.get("confirm", "")
        if not check_password_hash(u["password_hash"], cur):
            flash("Current password is wrong.", "error")
        elif not valid_password(new) or new != new2:
            flash("New passwords must match and have 8+ characters with a letter and a number.", "error")
        else:
            execute("UPDATE users SET password_hash=%s WHERE user_id=%s", (generate_password_hash(new), g.user["user_id"]))
            audit(g.user["user_id"], "PASSWORD_CHANGED")
            flash("Password changed.", "ok")
        return redirect(url_for("profile"))
    return render_template("profile.html")


# ================================================================ admin
@app.route("/admin")
@login_required("admin")
def admin():
    stats = query("SELECT (SELECT COUNT(*) FROM users) AS users, (SELECT COUNT(*) FROM patient) AS patients, "
                  "(SELECT COUNT(*) FROM test_report) AS reports, (SELECT COUNT(*) FROM test_report WHERE is_demo=0) AS real_reports, "
                  "(SELECT COUNT(*) FROM alert) AS alerts", one=True)
    per_day = query("SELECT DATE(created_at) AS d, COUNT(*) AS n FROM test_report WHERE created_at > NOW() - INTERVAL 30 DAY "
                    "GROUP BY DATE(created_at) ORDER BY d")
    top = query("SELECT a.name, COUNT(*) AS n FROM diagnostic_result d JOIN analyte a ON a.analyte_id=d.analyte_id "
                "WHERE d.status<>'Normal' GROUP BY a.name ORDER BY n DESC LIMIT 5")
    users = query("SELECT user_id, full_name, email, role, is_active, created_at FROM users ORDER BY created_at DESC LIMIT 100")
    logs = query("SELECT l.*, u.full_name FROM audit_log l LEFT JOIN users u ON u.user_id=l.user_id "
                 "ORDER BY l.log_id DESC LIMIT 30")
    refs = query("SELECT a.name, a.pad_order, c.level_index, c.level_label, c.r, c.g, c.b FROM reference_color c "
                 "JOIN analyte a ON a.analyte_id=c.analyte_id ORDER BY a.pad_order, c.level_index")
    chart = {}
    for r in refs:
        chart.setdefault(r["name"], []).append(r)
    return render_template("admin.html", stats=stats, per_day=per_day, top=top, users=users, logs=logs,
                           chart=chart, features=DB_FEATURES)


@app.route("/admin/user/<int:uid>", methods=["POST"])
@login_required("admin")
def admin_user(uid):
    if uid == g.user["user_id"]:
        flash("You can't change your own account here.", "warn")
        return redirect(url_for("admin"))
    action = request.form.get("action")
    if action == "toggle":
        execute("UPDATE users SET is_active = 1 - is_active WHERE user_id=%s", (uid,))
    elif action in ("patient", "labtech", "clinician", "admin"):
        execute("UPDATE users SET role=%s WHERE user_id=%s", (action, uid), commit=False)
        table = {"patient": ("patient", "patient_id"), "clinician": ("clinician", "clinician_id"),
                 "labtech": ("lab_technician", "labtech_id")}.get(action)
        if table:
            execute(f"INSERT IGNORE INTO {table[0]} ({table[1]}) VALUES (%s)", (uid,), commit=False)
        get_db().commit()
    audit(g.user["user_id"], "ADMIN_USER_CHANGE", f"user={uid} action={action}")
    flash("User updated.", "ok")
    return redirect(url_for("admin"))


# ================================================================ errors
@app.errorhandler(403)
def e403(e):
    return render_template("error.html", title="Not allowed", message="Your role doesn't have access to this page."), 403


@app.errorhandler(404)
def e404(e):
    return render_template("error.html", title="Not found", message="That page or report doesn't exist."), 404


@app.errorhandler(400)
def e400(e):
    return render_template("error.html", title="Please try again", message=getattr(e, "description", "Bad request")), 400


@app.errorhandler(413)
def e413(e):
    return render_template("error.html", title="Photo too large", message="Please upload a photo under 12 MB."), 413


@app.template_filter("dt")
def fmt_dt(v, f="%d %b %Y, %I:%M %p"):
    return v.strftime(f) if v else ""


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
