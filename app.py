"""UroScan - Smart Urine Strip Reader with a Healthcare Database.

Flask web app for a DBMS mini-project:
  * patients and doctors log in (passwords are hashed)
  * a patient uploads a photo of a urine strip -> vision.py reads the 10 pads with KNN colour matching
  * the result is saved in MySQL in ONE transaction; triggers mark each test Normal / Trace / High / Low
  * the patient sees a simple report, an AI summary, a trend graph and can chat with the AI
  * a doctor picks a patient, opens a report and adds notes
"""
import io
import os
import re
import secrets
import logging
from datetime import datetime, timedelta
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


# ---------------------------------------------------------------- helpers
def normal_range(analyte):
    """Text like 'Negative' or '5.0 - 8.0' built from the chart levels that count as normal."""
    _, mn, mx, _ = ANALYTE_INFO[analyte]
    labels = [label for label, value, _ in CHART[analyte] if mn <= value <= mx]
    return labels[0] if len(labels) == 1 else f"{labels[0]} – {labels[-1]}"


def status_in_python(analyte, label, value):
    """Same rule as the trigger trg_set_status (only used if the server doesn't allow triggers)."""
    _, mn, mx, _ = ANALYTE_INFO[analyte]
    if mn <= value <= mx:
        return "Normal"
    if label.startswith("Trace"):
        return "Trace"
    return "Low" if value < mn else "High"


@app.before_request
def load_user():
    global DB_READY
    if not DB_READY and request.endpoint != "static":
        try:
            init_db()
            DB_READY = True
        except Exception:
            return render_template("error.html", title="Database not reachable",
                                   message="UroScan can't reach its database right now. Please try again in a minute."), 503
    g.user = None
    if session.get("uid"):
        g.user = query("SELECT user_id, full_name, email, role FROM users WHERE user_id=%s", (session["uid"],), one=True)
        if not g.user:
            session.clear()
    # every form carries a secret token so other websites can't submit forms for you (CSRF protection)
    if request.method == "POST" and request.endpoint != "api_chat":
        if request.form.get("csrf_token") != session.get("csrf_token"):
            abort(400, "This form expired. Please go back, refresh the page and try again.")


@app.context_processor
def template_values():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return dict(csrf_token=session["csrf_token"], user=g.get("user"), ai_on=ai.ai_available())


def login_required(role=None):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not g.user:
                flash("Please log in first.", "warn")
                return redirect(url_for("login", next=request.path))
            if role and g.user["role"] != role:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator


def can_see(patient_id):
    return g.user and (g.user["role"] == "doctor" or g.user["user_id"] == patient_id)


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
            cur.execute("UPDATE test_report SET abnormal_count = (SELECT COUNT(*) FROM test_result "
                        "WHERE report_id=%s AND status<>'Normal') WHERE report_id=%s", (report_id, report_id))
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


def report_results(report_id):
    rows = query("SELECT a.name AS analyte, a.unit, t.level_label, t.approx_value, t.status, t.r, t.g, t.b "
                 "FROM test_result t JOIN analyte a ON a.analyte_id = t.analyte_id "
                 "WHERE t.report_id=%s ORDER BY a.pad_order", (report_id,))
    for r in rows:
        r["normal"] = normal_range(r["analyte"])
        r["measured"] = None                       # colour -> number, e.g. "about 180 mg/dL"
        if r["analyte"] in MEASURED and r["approx_value"] is not None:
            number = f"{float(r['approx_value']):.{MEASURED[r['analyte']]}f}"
            r["measured"] = f"{number} {r['unit'] or ''}".strip()
    return rows


def patient_reports(patient_id):
    return query("SELECT report_id, created_at, abnormal_count FROM test_report "
                 "WHERE patient_id=%s ORDER BY created_at DESC, report_id DESC", (patient_id,))


# ---------------------------------------------------------------- pages anyone can see
@app.route("/")
def index():
    return redirect(url_for("dashboard")) if g.user else render_template("landing.html")


@app.route("/health")
def health():
    query("SELECT 1 AS ok")
    return "ok"


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("dashboard"))
    form = request.form
    if request.method == "POST":
        name = form.get("full_name", "").strip()
        email = form.get("email", "").strip().lower()
        password, confirm = form.get("password", ""), form.get("confirm", "")
        role = form.get("role", "patient")
        errors = []
        if len(name) < 2:
            errors.append("Please enter your full name.")
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            errors.append("Please enter a valid email.")
        if len(password) < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
            errors.append("Password must be at least 8 characters with a letter and a number.")
        if password != confirm:
            errors.append("Passwords don't match.")
        if role not in ("patient", "doctor"):
            errors.append("Please choose Patient or Doctor.")
        if not errors and query("SELECT 1 FROM users WHERE email=%s", (email,), one=True):
            errors.append("An account with this email already exists. Try logging in.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register.html", form=form)
        uid = execute("INSERT INTO users (full_name, email, password_hash, role) VALUES (%s,%s,%s,%s)",
                      (name, email, generate_password_hash(password), role))
        session.clear()
        session["uid"] = uid
        flash(f"Welcome to UroScan, {name.split()[0]}!", "ok")
        return redirect(url_for("dashboard"))
    return render_template("register.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
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
        session.clear()
        session["uid"] = u["user_id"]
        session.permanent = bool(request.form.get("remember"))
        nxt = request.args.get("next", "")
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
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
        u = query("SELECT user_id FROM users WHERE email=%s", (request.form.get("email", "").strip().lower(),), one=True)
        if u:
            token = secrets.token_hex(32)
            execute("INSERT INTO password_reset (token, user_id, expires_at) VALUES (%s,%s,%s)",
                    (token, u["user_id"], datetime.now() + timedelta(minutes=30)))
            link = url_for("reset", token=token, _external=True)
        flash("If that email is registered, a reset link has been created (valid for 30 minutes).", "ok")
    return render_template("forgot.html", link=link)


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    row = query("SELECT * FROM password_reset WHERE token=%s AND used=0 AND expires_at > NOW()", (token,), one=True)
    if not row:
        flash("This reset link is invalid or has expired.", "error")
        return redirect(url_for("forgot"))
    if request.method == "POST":
        pw, pw2 = request.form.get("password", ""), request.form.get("confirm", "")
        if len(pw) < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw) or pw != pw2:
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
    if g.user["role"] == "doctor":
        return redirect(url_for("patients"))
    pid = g.user["user_id"]
    reports = patient_reports(pid)
    latest = query("SELECT * FROM test_report WHERE report_id=%s", (reports[0]["report_id"],), one=True) if reports else None
    results = report_results(latest["report_id"]) if latest else []
    notes = query("SELECT n.note, n.created_at, n.report_id, u.full_name FROM doctor_note n "
                  "JOIN users u ON u.user_id = n.doctor_id JOIN test_report r ON r.report_id = n.report_id "
                  "WHERE r.patient_id=%s ORDER BY n.created_at DESC LIMIT 3", (pid,))
    return render_template("dashboard.html", reports=reports, latest=latest, results=results, notes=notes,
                           analytes=PAD_ORDER, pid=pid)


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
    rep = query("SELECT r.*, u.full_name AS patient_name FROM test_report r JOIN users u ON u.user_id = r.patient_id "
                "WHERE r.report_id=%s", (report_id,), one=True)
    if not rep or not can_see(rep["patient_id"]):
        abort(404)
    notes = query("SELECT n.*, u.full_name FROM doctor_note n JOIN users u ON u.user_id = n.doctor_id "
                  "WHERE n.report_id=%s ORDER BY n.created_at", (report_id,))
    return render_template("report.html", rep=rep, results=report_results(report_id), notes=notes)


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
                   points=[{"date": r["created_at"].strftime("%d %b"), "y": r["level_index"], "label": r["level_label"],
                            "status": r["status"], "id": r["report_id"]} for r in rows])


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
    history = [(r["created_at"].strftime("%Y-%m-%d"), r["name"], r["level_label"], r["status"]) for r in rows]
    reports = patient_reports(pid)
    latest = report_results(reports[0]["report_id"]) if reports else []
    return jsonify(answer=ai.chat(question, history, latest))


# ---------------------------------------------------------------- doctor
@app.route("/patients")
@login_required("doctor")
def patients():
    rows = query("SELECT * FROM v_patient_summary ORDER BY last_scan IS NULL, last_scan DESC")
    return render_template("clinician.html", rows=rows)


@app.route("/patients/<int:pid>")
@login_required("doctor")
def patient_detail(pid):
    patient = query("SELECT user_id, full_name, email FROM users WHERE user_id=%s AND role='patient'", (pid,), one=True)
    if not patient:
        abort(404)
    return render_template("history.html", patient=patient, reports=patient_reports(pid), analytes=PAD_ORDER)


@app.route("/report/<int:report_id>/note", methods=["POST"])
@login_required("doctor")
def add_note(report_id):
    note = request.form.get("note", "").strip()
    if note:
        execute("INSERT INTO doctor_note (report_id, doctor_id, note) VALUES (%s,%s,%s)",
                (report_id, g.user["user_id"], note[:2000]))
        flash("Note added.", "ok")
    return redirect(url_for("report", report_id=report_id))


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
    return value.strftime(fmt) if value else ""


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
