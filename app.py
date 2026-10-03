"""UroScan - Smart Urine Strip Reader with a Healthcare Database.

Flask web app for a DBMS mini-project with three roles:
  * patient   - signs up, uploads a strip photo (read with KNN colour matching), sees reports, graphs and an AI chat
  * clinician - created by the admin; reviews reports (status, follow-up date, notes) and corrects misread pads
  * admin     - created from settings; adds clinicians, resets passwords, disables accounts, assigns patients
"""
import io
import os
import re
import secrets
import logging
import random
from datetime import datetime, timedelta, date
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for, session, flash,
                   jsonify, abort, send_file, g)
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_db, query, execute, close_db, init_db, DB_FEATURES
from reference import CHART, PAD_ORDER, ANALYTE_INFO, MEASURED
import vision
import ai

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("uroscan")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,            # photos up to 12 MB
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
app.teardown_appcontext(close_db)

DB_READY = False
try:
    init_db()
    DB_READY = True
except Exception as e:
    log.error("Database not reachable at start-up: %s", e)

REVIEW_STATUSES = ["Pending", "Reviewed", "Follow-up", "Urgent"]


# ---------------------------------------------------------------- helpers
# The database server runs on UTC; show every date in Indian time (IST = UTC + 5:30)
TZ_OFFSET = timedelta(minutes=int(os.environ.get("TZ_OFFSET_MINUTES", 330)))


def local(value):
    return value + TZ_OFFSET if isinstance(value, datetime) else value


def today_local():
    return (datetime.utcnow() + TZ_OFFSET).date()


def normal_range(analyte):
    """Text like 'Negative' or '5.0 - 8.0' built from the chart levels that count as normal."""
    _, mn, mx, _ = ANALYTE_INFO[analyte]
    labels = [label for label, value, _ in CHART[analyte] if mn <= value <= mx]
    return labels[0] if len(labels) == 1 else f"{labels[0]} – {labels[-1]}"


def status_in_python(analyte, label, value):
    """Same rule as the trigger trg_set_status (used as a fallback and when a reading is corrected)."""
    _, mn, mx, _ = ANALYTE_INFO[analyte]
    if mn <= value <= mx:
        return "Normal"
    if label.startswith("Trace"):
        return "Trace"
    return "Low" if value < mn else "High"


def valid_password(pw):
    return len(pw) >= 8 and re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw)


def doctor_name(name):
    """Clinician names always start with 'Dr' (typed 'asha mehta', 'dr. Asha' or 'Dr Asha' -> 'Dr Asha ...')."""
    name = re.sub(r"^\s*dr\.?\s+", "", name.strip(), flags=re.I).strip()
    return f"Dr {name}" if name else name


def valid_email(email):
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email)


@app.before_request
def load_user():
    global DB_READY
    if not DB_READY and request.endpoint != "static":
        try:
            init_db()
            DB_READY = True
        except Exception:
            return render_template("error.html", title="We'll be right back",
                                   message="uroscan is starting up or under maintenance. Please try again in a minute."), 503
    g.user = None
    if session.get("uid") and request.endpoint != "static":
        g.user = query("SELECT user_id, full_name, email, role, is_active, dob, gender FROM users WHERE user_id=%s",
                       (session["uid"],), one=True)
        if not g.user or not g.user["is_active"]:
            g.user = None
            session.clear()
    # every form carries a secret token so other websites can't submit forms for you (CSRF protection)
    if request.method == "POST" and request.endpoint != "api_chat":
        if request.form.get("csrf_token") != session.get("csrf_token"):
            abort(400, "This form expired. Please go back, refresh the page and try again.")


@app.context_processor
def template_values():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return dict(csrf_token=session["csrf_token"], user=g.get("user"), ai_on=ai.ai_available(),
                now=datetime.utcnow(), today=today_local(), REVIEW_STATUSES=REVIEW_STATUSES)


def login_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not g.user:
                flash("Please log in first.", "warn")
                return redirect(url_for("login", next=request.path))
            if roles and g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator


def can_see(patient_id):
    return g.user and (g.user["role"] in ("clinician", "admin") or g.user["user_id"] == patient_id)


def as_actor():
    """Tell the audit triggers who is doing the next change (MySQL session variable @actor_id)."""
    execute("SET @actor_id = %s", (g.user["user_id"],), commit=False)


def home_for(user):
    return {"admin": "admin_home", "clinician": "patients"}.get(user["role"], "dashboard")


# ---------------------------------------------------------------- saving a scan (ACID transaction)
def save_scan(patient_id, readings, image):
    """Save the report and its 10 results together: either everything is saved or nothing is."""
    conn = get_db()
    cur = conn.cursor()
    analyte_ids = {r["name"]: r["analyte_id"] for r in query("SELECT analyte_id, name FROM analyte")}
    try:
        conn.begin()                                                                  # START TRANSACTION
        cur.execute("INSERT INTO test_report (patient_id, strip_image) VALUES (%s, %s)", (patient_id, image))
        report_id = cur.lastrowid
        for r in readings:
            cur.execute("INSERT INTO test_result (report_id, analyte_id, level_label, level_index, numeric_value, "
                        "approx_value, r, g, b, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (report_id, analyte_ids[r["analyte"]], r["level_label"], r["level_index"], r["value"],
                         r["approx"], *r["rgb"], status_in_python(r["analyte"], r["level_label"], r["value"])))
        if not DB_FEATURES["triggers"]:                                               # fallback for the trigger
            recount(cur, report_id)
        conn.commit()                                                                 # COMMIT
    except Exception:
        conn.rollback()                                                               # ROLLBACK: nothing half-saved
        raise
    finally:
        cur.close()

    results = report_results(report_id)
    summary, _ = ai.summarise(results)
    execute("UPDATE test_report SET ai_summary=%s WHERE report_id=%s", (summary, report_id))
    return report_id


def recount(cur, report_id):
    cur.execute("UPDATE test_report SET abnormal_count = (SELECT COUNT(*) FROM test_result "
                "WHERE report_id=%s AND status<>'Normal') WHERE report_id=%s", (report_id, report_id))


def report_results(report_id):
    rows = query("SELECT a.name AS analyte, a.unit, t.level_label, t.level_index, t.approx_value, t.status, "
                 "t.r, t.g, t.b, t.original_label, t.corrected_at, c.full_name AS corrected_by_name "
                 "FROM test_result t JOIN analyte a ON a.analyte_id = t.analyte_id "
                 "LEFT JOIN users c ON c.user_id = t.corrected_by "
                 "WHERE t.report_id=%s ORDER BY a.pad_order", (report_id,))
    for r in rows:
        r["normal"] = normal_range(r["analyte"])
        r["measured"] = None                       # colour -> number, e.g. "about 180 mg/dL"
        if r["analyte"] in MEASURED and r["approx_value"] is not None:
            number = f"{float(r['approx_value']):.{MEASURED[r['analyte']]}f}"
            r["measured"] = f"{number} {r['unit'] or ''}".strip()
        # the colour chart for this test (every level), so the page can ring the matched colour
        r["chart"] = [{"label": label, "hex": "#%02x%02x%02x" % tuple(rgb)} for label, _, rgb in CHART[r["analyte"]]]
    return rows


def compare_with_previous(report, results):
    """Adds r['prev'] and r['change'] (up / down / same) using the patient's scan just before this one."""
    prev = query("SELECT report_id, created_at FROM test_report WHERE patient_id=%s AND "
                 "(created_at < %s OR (created_at = %s AND report_id < %s)) "
                 "ORDER BY created_at DESC, report_id DESC LIMIT 1",
                 (report["patient_id"], report["created_at"], report["created_at"], report["report_id"]), one=True)
    if not prev:
        return None
    before = {r["analyte"]: r for r in report_results(prev["report_id"])}
    for r in results:
        p = before.get(r["analyte"])
        if p:
            r["prev"] = p["measured"] or p["level_label"]
            diff = r["level_index"] - p["level_index"]
            r["change"] = "up" if diff > 0 else "down" if diff < 0 else "same"
    return prev


def test_grid(patient_id, limit=8):
    """Rows = the 10 tests, columns = the last few scans, cell = status. One JOIN query."""
    rows = query("SELECT r.report_id, r.created_at, a.name, a.pad_order, t.level_label, t.status "
                 "FROM test_report r JOIN test_result t ON t.report_id = r.report_id "
                 "JOIN analyte a ON a.analyte_id = t.analyte_id "
                 "WHERE r.report_id IN (SELECT report_id FROM (SELECT report_id FROM test_report WHERE patient_id=%s "
                 "ORDER BY created_at DESC, report_id DESC LIMIT %s) AS last_scans) "
                 "ORDER BY r.created_at, r.report_id, a.pad_order", (patient_id, limit))
    scans, cells = [], {}
    for row in rows:
        if not scans or scans[-1]["id"] != row["report_id"]:
            scans.append({"id": row["report_id"], "date": local(row["created_at"])})
        cells[(row["name"], row["report_id"])] = row
    grid = [{"analyte": a, "cells": [cells.get((a, s["id"])) for s in scans]} for a in PAD_ORDER]
    return {"scans": scans, "rows": grid}


GAUGE_STOPS = [(159, 214, 186), (156, 200, 238), (182, 166, 238)]      # mint -> sky -> lavender


@app.template_global()
def gauge(value, n=22):
    """Half-circle of rounded bars (like a speedometer). value 0..1 -> list of bars with angle and colour."""
    value = max(0.0, min(1.0, float(value or 0)))
    lit = round(value * n)
    bars = []
    for i in range(n):
        t = i / (n - 1)
        a, b = (GAUGE_STOPS[0], GAUGE_STOPS[1]) if t < .5 else (GAUGE_STOPS[1], GAUGE_STOPS[2])
        k = t * 2 if t < .5 else (t - .5) * 2
        rgb = tuple(round(x + (y - x) * k) for x, y in zip(a, b))
        bars.append({"angle": round(-90 + (i + .5) * 180 / n, 2), "on": i < lit, "color": "#%02x%02x%02x" % rgb})
    return bars


def age_of(dob):
    if not dob:
        return None
    t = date.today()
    return t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day))


def check_profile(dob, gender):
    errors = []
    if dob:
        try:
            d = datetime.strptime(dob, "%Y-%m-%d").date()
            if d > date.today() or d.year < 1900:
                errors.append("Please enter a real date of birth.")
        except ValueError:
            errors.append("Please enter a valid date of birth.")
    if gender and gender not in ("Male", "Female", "Other"):
        errors.append("Please choose a gender from the list.")
    return errors


def patient_reports(patient_id):
    return query("SELECT report_id, created_at, abnormal_count, review_status, follow_up_date FROM test_report "
                 "WHERE patient_id=%s ORDER BY created_at DESC, report_id DESC", (patient_id,))


# ---------------------------------------------------------------- pages anyone can see
@app.route("/")
def index():
    return redirect(url_for(home_for(g.user))) if g.user else render_template("landing.html")


@app.route("/health")
def health():
    query("SELECT 1 AS ok")
    return "ok"


@app.route("/register", methods=["GET", "POST"])
def register():
    """Only patients sign up. Clinician accounts are created by the admin."""
    if g.user:
        return redirect(url_for(home_for(g.user)))
    form = request.form
    if request.method == "POST":
        name = form.get("full_name", "").strip()
        email = form.get("email", "").strip().lower()
        password, confirm = form.get("password", ""), form.get("confirm", "")
        dob, gender = form.get("dob") or None, form.get("gender") or None
        errors = []
        if len(name) < 2:
            errors.append("Please enter your full name.")
        if not valid_email(email):
            errors.append("Please enter a valid email.")
        if not valid_password(password):
            errors.append("Password must be at least 8 characters with a letter and a number.")
        if password != confirm:
            errors.append("Passwords don't match.")
        errors += check_profile(dob, gender)
        if not errors and query("SELECT 1 FROM users WHERE email=%s", (email,), one=True):
            errors.append("An account with this email already exists. Try logging in.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register.html", form=form)
        uid = execute("INSERT INTO users (full_name, email, password_hash, role, dob, gender) VALUES (%s,%s,%s,'patient',%s,%s)",
                      (name, email, generate_password_hash(password), dob, gender))
        session.clear()
        session["uid"] = uid
        flash(f"Welcome to uroscan, {name.split()[0]}.", "ok")
        return redirect(url_for("dashboard"))
    return render_template("register.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for(home_for(g.user)))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        recent_fails = query("SELECT COUNT(*) AS n FROM login_attempt WHERE email=%s AND success=0 "
                             "AND attempted_at > NOW() - INTERVAL 15 MINUTE", (email,), one=True)["n"]
        if recent_fails >= 5:
            flash("Too many wrong attempts. Please wait 15 minutes or reset your password.", "error")
            return render_template("login.html", email=email)
        u = query("SELECT * FROM users WHERE email=%s", (email,), one=True)
        ok = bool(u and check_password_hash(u["password_hash"], password))
        execute("INSERT INTO login_attempt (email, success, ip) VALUES (%s,%s,%s)",
                (email, int(ok), (request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:45]))
        if not ok:
            flash("Wrong email or password.", "error")
            return render_template("login.html", email=email)
        if not u["is_active"]:
            flash("This account has been switched off. Please contact your clinic.", "error")
            return render_template("login.html", email=email)
        session.clear()
        session["uid"] = u["user_id"]
        session.permanent = bool(request.form.get("remember"))
        nxt = request.args.get("next", "")
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for(home_for(u)))
    return render_template("login.html", email="")


@app.route("/logout")
def logout():
    session.clear()
    flash("You've been logged out.", "ok")
    return redirect(url_for("login"))


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    link = None
    if request.method == "POST":
        u = query("SELECT user_id, role FROM users WHERE email=%s", (request.form.get("email", "").strip().lower(),), one=True)
        if u and u["role"] == "patient":
            token = secrets.token_hex(32)
            execute("INSERT INTO password_reset (token, user_id, expires_at) VALUES (%s,%s,%s)",
                    (token, u["user_id"], datetime.utcnow() + timedelta(minutes=30)))
            link = url_for("reset", token=token, _external=True)
        flash("If that email has a uroscan account, a reset link has been created. It works for 30 minutes.", "ok")
    return render_template("forgot.html", link=link)


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    row = query("SELECT * FROM password_reset WHERE token=%s AND used=0 AND expires_at > UTC_TIMESTAMP()", (token,), one=True)
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
        flash("Password updated. Please log in.", "ok")
        return redirect(url_for("login"))
    return render_template("reset.html")


# ---------------------------------------------------------------- patient
@app.route("/dashboard")
@login_required()
def dashboard():
    if g.user["role"] != "patient":
        return redirect(url_for(home_for(g.user)))
    pid = g.user["user_id"]
    reports = patient_reports(pid)
    latest = query("SELECT * FROM test_report WHERE report_id=%s", (reports[0]["report_id"],), one=True) if reports else None
    results = report_results(latest["report_id"]) if latest else []
    prev = compare_with_previous(latest, results) if latest else None
    notes = query("SELECT n.note, n.created_at, n.report_id, u.full_name FROM clinician_note n "
                  "JOIN users u ON u.user_id = n.clinician_id JOIN test_report r ON r.report_id = n.report_id "
                  "WHERE r.patient_id=%s ORDER BY n.created_at DESC LIMIT 3", (pid,))
    follow_up = query("SELECT r.report_id, r.follow_up_date, r.review_status, u.full_name AS clinician "
                      "FROM test_report r LEFT JOIN users u ON u.user_id = r.reviewed_by "
                      "WHERE r.patient_id=%s AND r.follow_up_date IS NOT NULL AND r.review_status IN ('Follow-up','Urgent') "
                      "ORDER BY r.created_at DESC LIMIT 1", (pid,), one=True)
    return render_template("dashboard.html", reports=reports, latest=latest, results=results, notes=notes,
                           prev=prev, analytes=PAD_ORDER, pid=pid, grid=test_grid(pid), follow_up=follow_up)


@app.route("/scan", methods=["GET", "POST"])
@login_required("patient")
def scan():
    if request.method == "POST":
        sample = request.form.get("sample", "")
        if re.fullmatch(r"[1-6]", sample):
            with open(os.path.join(app.static_folder, "samples", f"sample_{sample}.jpg"), "rb") as fh:
                photo = fh.read()
        else:
            f = request.files.get("photo")
            if not f or not f.filename:
                flash("Please choose or take a photo of the strip.", "error")
                return render_template("scan.html")
            photo = f.read()
        try:
            out = vision.analyse(photo)                       # read the 10 pads (KNN colour matching)
        except vision.ScanError as e:
            flash(str(e), "error")
            return render_template("scan.html")
        report_id = save_scan(g.user["user_id"], out["results"], out["image"])
        return redirect(url_for("report", report_id=report_id))
    return render_template("scan.html")


@app.route("/report/<int:report_id>")
@login_required()
def report(report_id):
    rep = query("SELECT r.*, u.full_name AS patient_name, u.dob, u.gender, u.email AS patient_email, "
                "rv.full_name AS reviewer FROM test_report r JOIN users u ON u.user_id = r.patient_id "
                "LEFT JOIN users rv ON rv.user_id = r.reviewed_by WHERE r.report_id=%s", (report_id,), one=True)
    if not rep or not can_see(rep["patient_id"]):
        abort(404)
    notes = query("SELECT n.*, u.full_name FROM clinician_note n JOIN users u ON u.user_id = n.clinician_id "
                  "WHERE n.report_id=%s ORDER BY n.created_at", (report_id,))
    results = report_results(report_id)
    prev = compare_with_previous(rep, results)
    return render_template("report.html", rep=rep, results=results, notes=notes, prev=prev, age=age_of(rep["dob"]))


@app.route("/report/<int:report_id>/photo")
@login_required()
def report_photo(report_id):
    rep = query("SELECT patient_id, strip_image FROM test_report WHERE report_id=%s", (report_id,), one=True)
    if not rep or not can_see(rep["patient_id"]) or not rep["strip_image"]:
        abort(404)
    return send_file(io.BytesIO(rep["strip_image"]), mimetype="image/jpeg")


@app.route("/report/<int:report_id>/delete", methods=["POST"])
@login_required("patient")
def delete_report(report_id):
    execute("DELETE FROM test_report WHERE report_id=%s AND patient_id=%s", (report_id, g.user["user_id"]))
    flash("Report deleted.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/api/graph/<int:pid>")
@login_required()
def graph_data(pid):
    """Points for the trend graph, fetched with the stored procedure get_test_history."""
    if not can_see(pid):
        abort(403)
    test = request.args.get("test", "Glucose")
    if test not in CHART:
        abort(400)
    cur = get_db().cursor()
    cur.callproc("get_test_history", (pid, test))
    rows = cur.fetchall()
    cur.close()
    get_db().commit()
    normal = [i for i, (_, v, _) in enumerate(CHART[test]) if ANALYTE_INFO[test][1] <= v <= ANALYTE_INFO[test][2]]
    return jsonify(levels=[l for l, _, _ in CHART[test]], normal=[min(normal), max(normal)],
                   points=[{"date": local(r["created_at"]).strftime("%d %b"), "y": r["level_index"], "label": r["level_label"],
                            "status": r["status"], "id": r["report_id"]} for r in rows])


@app.route("/profile", methods=["GET", "POST"])
@login_required()
def profile():
    if request.method == "POST":
        name = request.form.get("full_name", "").strip()
        if g.user["role"] == "clinician":
            name = doctor_name(name)
        dob = request.form.get("dob") or None
        gender = request.form.get("gender") or None
        errors = [] if len(name) >= 2 else ["Please enter your full name."]
        errors += check_profile(dob, gender)
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            execute("UPDATE users SET full_name=%s, dob=%s, gender=%s WHERE user_id=%s", (name, dob, gender, g.user["user_id"]))
            flash("Profile saved.", "ok")
            return redirect(url_for("profile"))
    if g.user["role"] == "patient":
        stats = query("SELECT COUNT(*) AS reports, MIN(created_at) AS first_scan, MAX(created_at) AS last_scan "
                      "FROM test_report WHERE patient_id=%s", (g.user["user_id"],), one=True)
    elif g.user["role"] == "clinician":
        stats = query("SELECT (SELECT COUNT(*) FROM clinician_note WHERE clinician_id=%s) AS notes, "
                      "(SELECT COUNT(*) FROM test_report WHERE reviewed_by=%s) AS reviewed, "
                      "(SELECT COUNT(*) FROM patient_clinician WHERE clinician_id=%s) AS patients",
                      (g.user["user_id"],) * 3, one=True)
    else:
        stats = query("SELECT COUNT(*) AS actions FROM audit_log WHERE actor_id=%s", (g.user["user_id"],), one=True)
    return render_template("profile.html", stats=stats, age=age_of(g.user["dob"]))


@app.route("/api/chat", methods=["POST"])
@login_required()
def api_chat():
    data = request.get_json(silent=True) or {}
    if data.get("csrf_token") != session.get("csrf_token"):
        return jsonify(answer="Please refresh the page and try again."), 400
    question = (data.get("message") or "").strip()[:500]
    pid = g.user["user_id"] if g.user["role"] == "patient" else int(data.get("patient_id") or 0)
    if not question or not can_see(pid):
        return jsonify(answer="Ask me something about the results.")
    rows = query("SELECT r.created_at, a.name, t.level_label, t.status FROM test_report r "
                 "JOIN test_result t ON t.report_id = r.report_id JOIN analyte a ON a.analyte_id = t.analyte_id "
                 "WHERE r.patient_id=%s ORDER BY r.created_at, a.pad_order", (pid,))
    history = [(local(r["created_at"]).strftime("%d %b %Y"), r["name"], r["level_label"], r["status"]) for r in rows]
    reports = patient_reports(pid)
    latest = report_results(reports[0]["report_id"]) if reports else []
    turns = [t for t in (data.get("history") or [])[-6:] if isinstance(t, dict)]
    audience = "clinician" if g.user["role"] != "patient" else "patient"
    return jsonify(answer=ai.chat(question, history, latest, turns=turns, audience=audience))


# ---------------------------------------------------------------- clinician
@app.route("/patients")
@login_required("clinician")
def patients():
    me = g.user["user_id"]
    show_all = request.args.get("all") == "1"
    mine = {r["patient_id"] for r in query("SELECT patient_id FROM patient_clinician WHERE clinician_id=%s", (me,))}
    rows = query("SELECT * FROM v_patient_summary ORDER BY urgent DESC, to_review DESC, last_scan IS NULL, last_scan DESC")
    for r in rows:
        r["age"] = age_of(r["dob"])
        r["mine"] = r["patient_id"] in mine
    if not show_all:
        rows = [r for r in rows if r["mine"]]
    # worklist: reports that still need attention, for my patients (or everyone when "All patients" is on)
    focus = request.args.get("status", "open")
    where = {"open": "r.review_status IN ('Pending','Urgent','Follow-up')", "Pending": "r.review_status='Pending'",
             "Urgent": "r.review_status='Urgent'", "Follow-up": "r.review_status='Follow-up'",
             "overdue": "r.follow_up_date < %s AND r.review_status IN ('Follow-up','Urgent')"}.get(focus)
    if where is None:
        focus, where = "open", "r.review_status IN ('Pending','Urgent','Follow-up')"
    args = [today_local()] if focus == "overdue" else []
    scope = "" if show_all else " AND r.patient_id IN (SELECT patient_id FROM patient_clinician WHERE clinician_id=%s)"
    if not show_all:
        args.append(me)
    worklist = query("SELECT r.report_id, r.created_at, r.abnormal_count, r.review_status, r.follow_up_date, "
                     "u.user_id AS patient_id, u.full_name FROM test_report r JOIN users u ON u.user_id = r.patient_id "
                     f"WHERE {where}{scope} ORDER BY FIELD(r.review_status,'Urgent','Pending','Follow-up','Reviewed'), "
                     "r.created_at DESC LIMIT 50", tuple(args))
    my_ids = tuple(mine) or (0,)
    marks = ",".join(["%s"] * len(my_ids))
    stats = query(f"SELECT COUNT(DISTINCT u.user_id) AS patients, "
                  f"SUM(r.review_status='Pending') AS to_review, SUM(r.review_status='Urgent') AS urgent, "
                  f"SUM(r.review_status IN ('Follow-up','Urgent') AND r.follow_up_date < %s) AS overdue "
                  f"FROM users u LEFT JOIN test_report r ON r.patient_id = u.user_id WHERE u.user_id IN ({marks})",
                  (today_local(), *my_ids), one=True)
    stats = {k: int(v or 0) for k, v in stats.items()}
    stats["patients"] = len(mine)
    prog = query(f"SELECT COUNT(*) AS total, SUM(review_status <> 'Pending') AS done FROM test_report "
                 f"WHERE patient_id IN ({marks})", my_ids, one=True)
    stats["total"], stats["done"] = int(prog["total"] or 0), int(prog["done"] or 0)
    return render_template("clinician.html", rows=rows, worklist=worklist, stats=stats, show_all=show_all, focus=focus)


@app.route("/patients/<int:pid>")
@login_required("clinician", "admin")
def patient_detail(pid):
    patient = query("SELECT user_id, full_name, email, dob, gender, created_at FROM users "
                    "WHERE user_id=%s AND role='patient'", (pid,), one=True)
    if not patient:
        abort(404)
    reports = query("SELECT r.report_id, r.created_at, r.abnormal_count, r.review_status, r.follow_up_date, "
                    "COUNT(n.note_id) AS notes FROM test_report r LEFT JOIN clinician_note n ON n.report_id = r.report_id "
                    "WHERE r.patient_id=%s GROUP BY r.report_id, r.created_at, r.abnormal_count, r.review_status, "
                    "r.follow_up_date ORDER BY r.created_at DESC, r.report_id DESC", (pid,))
    clinicians = query("SELECT u.full_name FROM patient_clinician pc JOIN users u ON u.user_id = pc.clinician_id "
                       "WHERE pc.patient_id=%s ORDER BY u.full_name", (pid,))
    latest = report_results(reports[0]["report_id"]) if reports else []
    if reports:
        compare_with_previous(dict(reports[0], patient_id=pid), latest)
    return render_template("history.html", patient=patient, reports=reports, latest=latest, clinicians=clinicians,
                           age=age_of(patient["dob"]), analytes=PAD_ORDER, grid=test_grid(pid) if reports else None)


@app.route("/report/<int:report_id>/review", methods=["POST"])
@login_required("clinician")
def review_report(report_id):
    """Clinician sets the review status, an optional follow-up date and an optional note - in one transaction."""
    rep = query("SELECT report_id FROM test_report WHERE report_id=%s", (report_id,), one=True)
    if not rep:
        abort(404)
    status = request.form.get("review_status", "Reviewed")
    if status not in REVIEW_STATUSES:
        abort(400)
    follow = request.form.get("follow_up_date") or None
    if follow:
        try:
            if datetime.strptime(follow, "%Y-%m-%d").date() < today_local():
                flash("The follow-up date can't be in the past.", "error")
                return redirect(url_for("report", report_id=report_id))
        except ValueError:
            follow = None
    if status in ("Pending", "Reviewed"):
        follow = None
    note = request.form.get("note", "").strip()[:2000]
    execute("UPDATE test_report SET review_status=%s, follow_up_date=%s, reviewed_by=%s, reviewed_at=UTC_TIMESTAMP() "
            "WHERE report_id=%s", (status, follow, g.user["user_id"], report_id), commit=False)
    if note:
        execute("INSERT INTO clinician_note (report_id, clinician_id, note) VALUES (%s,%s,%s)",
                (report_id, g.user["user_id"], note), commit=False)
    get_db().commit()
    flash("Review saved." + (" Note added." if note else ""), "ok")
    return redirect(url_for("report", report_id=report_id))


@app.route("/report/<int:report_id>/correct", methods=["POST"])
@login_required("clinician")
def correct_reading(report_id):
    """Clinician fixes a misread pad. The camera's original reading is kept in original_label."""
    analyte = request.form.get("analyte", "")
    try:
        idx = int(request.form.get("level_index", ""))
    except ValueError:
        abort(400)
    if analyte not in CHART or not 0 <= idx < len(CHART[analyte]):
        abort(400)
    label, value, _ = CHART[analyte][idx]
    conn = get_db()
    cur = conn.cursor()
    try:
        conn.begin()
        cur.execute("UPDATE test_result t JOIN analyte a ON a.analyte_id = t.analyte_id "
                    "SET t.original_label = COALESCE(t.original_label, t.level_label), t.level_label=%s, t.level_index=%s, "
                    "t.numeric_value=%s, t.approx_value=%s, t.status=%s, t.corrected_by=%s, t.corrected_at=UTC_TIMESTAMP() "
                    "WHERE t.report_id=%s AND a.name=%s AND t.level_index <> %s",
                    (label, idx, value, value, status_in_python(analyte, label, value), g.user["user_id"],
                     report_id, analyte, idx))
        changed = cur.rowcount
        recount(cur, report_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    if changed:
        summary, _ = ai.summarise(report_results(report_id))
        execute("UPDATE test_report SET ai_summary=%s WHERE report_id=%s", (summary, report_id))
        flash(f"{analyte} corrected to {label}. The status and summary were updated.", "ok")
    else:
        flash("That reading was already at this level - nothing changed.", "warn")
    return redirect(url_for("report", report_id=report_id))


# ---------------------------------------------------------------- admin
@app.route("/admin")
@login_required("admin")
def admin_home():
    t = today_local()
    stats = query("SELECT (SELECT COUNT(*) FROM users WHERE role='patient') AS patients, "
                  "(SELECT COUNT(*) FROM users WHERE role='clinician' AND is_active=1) AS clinicians, "
                  "(SELECT COUNT(*) FROM test_report WHERE created_at >= UTC_TIMESTAMP() - INTERVAL 7 DAY) AS week_scans, "
                  "(SELECT COUNT(*) FROM test_report WHERE review_status='Pending') AS pending, "
                  "(SELECT COUNT(*) FROM test_report) AS reports, "
                  "(SELECT COUNT(*) FROM test_report WHERE review_status='Urgent') AS urgent, "
                  "(SELECT COUNT(*) FROM users u WHERE role='patient' AND NOT EXISTS "
                  "  (SELECT 1 FROM patient_clinician pc WHERE pc.patient_id = u.user_id)) AS unassigned", one=True)
    activity = query("SELECT l.*, a.full_name AS actor, u.full_name AS target FROM audit_log l "
                     "LEFT JOIN users a ON a.user_id = l.actor_id LEFT JOIN users u ON u.user_id = l.target_id "
                     "ORDER BY l.created_at DESC, l.log_id DESC LIMIT 12")
    per_day = query("SELECT DATE(created_at + INTERVAL %s MINUTE) AS d, COUNT(*) AS n FROM test_report "
                    "WHERE created_at >= UTC_TIMESTAMP() - INTERVAL 14 DAY GROUP BY d ORDER BY d",
                    (int(TZ_OFFSET.total_seconds() // 60),))
    counts = {r["d"]: r["n"] for r in per_day}
    days = [t - timedelta(days=i) for i in range(13, -1, -1)]
    chart = [{"label": d.strftime("%d %b"), "n": counts.get(d, 0)} for d in days]
    return render_template("admin_home.html", stats=stats, activity=activity, chart=chart)


@app.route("/admin/clinicians", methods=["GET", "POST"])
@login_required("admin")
def admin_clinicians():
    form = {}
    if request.method == "POST":
        form = request.form
        name = doctor_name(form.get("full_name", ""))
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")
        errors = []
        if len(name) < 5:
            errors.append("Please enter the clinician's full name.")
        if not valid_email(email):
            errors.append("Please enter a valid email.")
        if not valid_password(password):
            errors.append("Password must be at least 8 characters with a letter and a number.")
        if not errors and query("SELECT 1 FROM users WHERE email=%s", (email,), one=True):
            errors.append("That email is already used by another account.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            as_actor()
            execute("INSERT INTO users (full_name, email, password_hash, role) VALUES (%s,%s,%s,'clinician')",
                    (name, email, generate_password_hash(password)))
            flash(f"Clinician account created for {name}. Share the email and password with them.", "ok")
            return redirect(url_for("admin_clinicians"))
    rows = query("SELECT u.user_id, u.full_name, u.email, u.is_active, u.created_at, "
                 "(SELECT COUNT(*) FROM patient_clinician pc WHERE pc.clinician_id = u.user_id) AS patients, "
                 "(SELECT COUNT(*) FROM test_report r WHERE r.reviewed_by = u.user_id) AS reviewed, "
                 "(SELECT MAX(attempted_at) FROM login_attempt la WHERE la.email = u.email AND la.success = 1) AS last_login "
                 "FROM users u WHERE u.role='clinician' ORDER BY u.is_active DESC, u.full_name")
    return render_template("admin_clinicians.html", rows=rows, form=form)


def get_clinician(cid):
    c = query("SELECT user_id, full_name, email, is_active FROM users WHERE user_id=%s AND role='clinician'", (cid,), one=True)
    if not c:
        abort(404)
    return c


@app.route("/admin/clinicians/<int:cid>/password", methods=["POST"])
@login_required("admin")
def admin_reset_password(cid):
    c = get_clinician(cid)
    pw = request.form.get("password", "")
    if not valid_password(pw):
        flash("New password must be at least 8 characters with a letter and a number.", "error")
    else:
        as_actor()
        execute("UPDATE users SET password_hash=%s WHERE user_id=%s", (generate_password_hash(pw), cid))
        flash(f"Password for {c['full_name']} updated. Share the new password with them.", "ok")
    return redirect(url_for("admin_clinicians"))


@app.route("/admin/clinicians/<int:cid>/toggle", methods=["POST"])
@login_required("admin")
def admin_toggle(cid):
    c = get_clinician(cid)
    as_actor()
    execute("UPDATE users SET is_active = 1 - is_active WHERE user_id=%s", (cid,))
    flash(f"{c['full_name']} is now {'disabled' if c['is_active'] else 'active'}.", "ok")
    return redirect(url_for("admin_clinicians"))


@app.route("/admin/clinicians/<int:cid>/assign", methods=["GET", "POST"])
@login_required("admin")
def admin_assign(cid):
    c = get_clinician(cid)
    if request.method == "POST":
        wanted = {int(x) for x in request.form.getlist("patients") if x.isdigit()}
        current = {r["patient_id"] for r in query("SELECT patient_id FROM patient_clinician WHERE clinician_id=%s", (cid,))}
        valid = {r["user_id"] for r in query("SELECT user_id FROM users WHERE role='patient'")}
        as_actor()
        for pid in (wanted & valid) - current:
            execute("INSERT INTO patient_clinician (patient_id, clinician_id) VALUES (%s,%s)", (pid, cid), commit=False)
        for pid in current - wanted:
            execute("DELETE FROM patient_clinician WHERE patient_id=%s AND clinician_id=%s", (pid, cid), commit=False)
        get_db().commit()
        flash(f"Patients assigned to {c['full_name']} updated.", "ok")
        return redirect(url_for("admin_clinicians"))
    patients_ = query("SELECT u.user_id, u.full_name, u.email, u.dob, u.gender, "
                      "EXISTS (SELECT 1 FROM patient_clinician pc WHERE pc.patient_id=u.user_id AND pc.clinician_id=%s) AS assigned, "
                      "(SELECT GROUP_CONCAT(x.full_name ORDER BY x.full_name SEPARATOR ', ') FROM patient_clinician p2 "
                      " JOIN users x ON x.user_id = p2.clinician_id WHERE p2.patient_id = u.user_id) AS clinicians "
                      "FROM users u WHERE u.role='patient' ORDER BY u.full_name", (cid,))
    for p in patients_:
        p["age"] = age_of(p["dob"])
    return render_template("admin_assign.html", c=c, patients=patients_)


@app.route("/admin/patients")
@login_required("admin")
def admin_patients():
    rows = query("SELECT s.*, (SELECT GROUP_CONCAT(x.full_name ORDER BY x.full_name SEPARATOR ', ') "
                 "FROM patient_clinician pc JOIN users x ON x.user_id = pc.clinician_id WHERE pc.patient_id = s.patient_id) "
                 "AS clinicians, (SELECT GROUP_CONCAT(pc.clinician_id) FROM patient_clinician pc "
                 "WHERE pc.patient_id = s.patient_id) AS clinician_ids FROM v_patient_summary s ORDER BY s.full_name")
    for r in rows:
        r["age"] = age_of(r["dob"])
        r["cids"] = {int(x) for x in (r["clinician_ids"] or "").split(",") if x}
    doctors = query("SELECT user_id, full_name FROM users WHERE role='clinician' AND is_active=1 ORDER BY full_name")
    unassigned = sum(1 for r in rows if not r["cids"])
    return render_template("admin_patients.html", rows=rows, doctors=doctors, unassigned=unassigned)


@app.route("/admin/patients/<int:pid>/assign", methods=["POST"])
@login_required("admin")
def admin_assign_patient(pid):
    """Choose which clinicians look after one patient (or let the system pick one at random)."""
    p = query("SELECT user_id, full_name FROM users WHERE user_id=%s AND role='patient'", (pid,), one=True)
    if not p:
        abort(404)
    active = {r["user_id"] for r in query("SELECT user_id FROM users WHERE role='clinician' AND is_active=1")}
    current = {r["clinician_id"] for r in query("SELECT clinician_id FROM patient_clinician WHERE patient_id=%s", (pid,))}
    if request.form.get("random"):
        choices = sorted(active - current)
        if not choices:
            flash(f"Every active clinician already looks after {p['full_name']}.", "warn")
            return redirect(url_for("admin_patients"))
        wanted = current | {random.choice(choices)}
    else:
        wanted = {int(x) for x in request.form.getlist("clinicians") if x.isdigit()} & active
        wanted |= current - active                      # keep links to disabled clinicians untouched
    as_actor()
    for cid in wanted - current:
        execute("INSERT INTO patient_clinician (patient_id, clinician_id) VALUES (%s,%s)", (pid, cid), commit=False)
    for cid in current - wanted:
        execute("DELETE FROM patient_clinician WHERE patient_id=%s AND clinician_id=%s", (pid, cid), commit=False)
    get_db().commit()
    names = [r["full_name"] for r in query("SELECT u.full_name FROM patient_clinician pc JOIN users u ON u.user_id = pc.clinician_id "
                                           "WHERE pc.patient_id=%s ORDER BY u.full_name", (pid,))]
    flash(f"{p['full_name']} is now looked after by {', '.join(names)}." if names
          else f"{p['full_name']} has no clinician now.", "ok")
    return redirect(url_for("admin_patients"))


@app.route("/admin/patients/assign-random", methods=["POST"])
@login_required("admin")
def admin_assign_random():
    """Give every patient without a clinician one active clinician, picked at random
    (always from the clinicians with the fewest patients, so the work stays balanced)."""
    doctors = query("SELECT u.user_id, (SELECT COUNT(*) FROM patient_clinician pc WHERE pc.clinician_id = u.user_id) AS n "
                    "FROM users u WHERE u.role='clinician' AND u.is_active=1")
    if not doctors:
        flash("Add a clinician first, then patients can be assigned.", "warn")
        return redirect(url_for("admin_patients"))
    waiting = query("SELECT u.user_id FROM users u WHERE u.role='patient' AND NOT EXISTS "
                    "(SELECT 1 FROM patient_clinician pc WHERE pc.patient_id = u.user_id)")
    load = {d["user_id"]: d["n"] for d in doctors}
    as_actor()
    for row in waiting:
        least = min(load.values())
        cid = random.choice([c for c, n in load.items() if n == least])
        execute("INSERT INTO patient_clinician (patient_id, clinician_id) VALUES (%s,%s)", (row["user_id"], cid), commit=False)
        load[cid] += 1
    get_db().commit()
    n = len(waiting)
    flash(f"{n} patient{'s' if n != 1 else ''} assigned to a clinician at random." if n
          else "Every patient already has a clinician.", "ok")
    return redirect(url_for("admin_patients"))


# ---------------------------------------------------------------- errors and small helpers
@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", title="Not allowed", message="You don't have access to this page."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", title="Not found", message="That page or report doesn't exist."), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", title="Please try again", message=getattr(e, "description", "")), 400


@app.errorhandler(413)
def too_large(e):
    return render_template("error.html", title="Photo too large", message="Please upload a photo under 12 MB."), 413


@app.template_filter("dt")
def format_date(value, fmt="%d %b %Y, %I:%M %p"):
    return local(value).strftime(fmt) if value else ""


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
