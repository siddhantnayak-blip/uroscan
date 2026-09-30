"""MySQL connection helpers + creating the tables when the app starts."""
import os
import ssl
import logging
import pymysql
from pymysql.cursors import DictCursor
from flask import g

from reference import CHART, ANALYTE_INFO

log = logging.getLogger("uroscan.db")
DB_FEATURES = {"triggers": False}

# tables from the first (more complex) version of UroScan, removed once if found
OLD_TABLES = ["alert", "clinician_note", "diagnostic_result", "test_strip", "audit_log", "test_report",
              "urine_sample", "lab_technician", "clinician", "patient", "laboratory",
              "password_reset", "login_attempt", "reference_color", "analyte", "users"]


def _connect():
    kwargs = dict(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", 3306)),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "defaultdb"),
        cursorclass=DictCursor,
        autocommit=False,
        charset="utf8mb4",
        connect_timeout=15,
    )
    if os.environ.get("DB_SSL", "1") == "1":        # Aiven requires an encrypted connection
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl"] = ctx
    return pymysql.connect(**kwargs)


def get_db():
    if "db" not in g:
        g.db = _connect()
    else:
        g.db.ping(reconnect=True)
    return g.db


def close_db(_=None):
    db = g.pop("db", None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


def query(sql, args=None, one=False):
    """Run a SELECT and return rows as dictionaries."""
    cur = get_db().cursor()
    cur.execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=None, commit=True):
    """Run INSERT / UPDATE / DELETE."""
    db = get_db()
    cur = db.cursor()
    cur.execute(sql, args)
    last = cur.lastrowid
    cur.close()
    if commit:
        db.commit()
    return last


def _exists(cur, sql, args):
    cur.execute(sql, args)
    return cur.fetchone()["n"] > 0


def upgrade_v2(cur, conn):
    """Small ALTER TABLE upgrades from the previous version, keeping all existing data:
    'doctor' becomes 'clinician', and patients get date of birth + gender."""
    t = "SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s"
    c = ("SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
         "AND TABLE_NAME = %s AND COLUMN_NAME = %s")
    if not _exists(cur, t, ("users",)):
        return                                        # brand-new database, schema.sql creates everything
    if _exists(cur, t, ("doctor_note",)) and not _exists(cur, t, ("clinician_note",)):
        cur.execute("RENAME TABLE doctor_note TO clinician_note")
        cur.execute("ALTER TABLE clinician_note RENAME COLUMN doctor_id TO clinician_id")
    cur.execute("SELECT COLUMN_TYPE AS ct FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'users' AND COLUMN_NAME = 'role'")
    if "doctor" in cur.fetchone()["ct"]:
        cur.execute("ALTER TABLE users MODIFY role ENUM('patient','doctor','clinician') NOT NULL DEFAULT 'patient'")
        cur.execute("UPDATE users SET role = 'clinician' WHERE role = 'doctor'")
        cur.execute("ALTER TABLE users MODIFY role ENUM('patient','clinician') NOT NULL DEFAULT 'patient'")
    if not _exists(cur, c, ("users", "dob")):
        cur.execute("ALTER TABLE users ADD COLUMN dob DATE NULL AFTER role")
    if not _exists(cur, c, ("users", "gender")):
        cur.execute("ALTER TABLE users ADD COLUMN gender ENUM('Male','Female','Other') NULL AFTER dob")
    conn.commit()


def init_db():
    """Create tables, view, triggers and procedure from schema.sql, then fill the colour chart."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT GET_LOCK('uroscan_init', 60)")
    try:
        cur.execute("SET GLOBAL log_bin_trust_function_creators = 1")
    except Exception:
        pass

    # one-time clean-up of the old version's tables (they had a different design)
    cur.execute("SELECT COUNT(*) AS n FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'urine_sample'")
    if cur.fetchone()["n"]:
        log.warning("Old UroScan tables found - removing them and creating the new design")
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for v in ("v_report_summary", "v_latest_results"):
            cur.execute(f"DROP VIEW IF EXISTS {v}")
        for t in OLD_TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {t}")
        for t in ("trg_result_status", "trg_result_alert", "trg_report_audit"):
            cur.execute(f"DROP TRIGGER IF EXISTS {t}")
        cur.execute("DROP PROCEDURE IF EXISTS get_patient_trend")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()

    upgrade_v2(cur, conn)

    path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(path, encoding="utf-8") as fh:
        parts = fh.read().split("-- @@")
    for stmt in parts:
        body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
        if not body:
            continue
        try:
            cur.execute(body)
            conn.commit()
        except pymysql.err.OperationalError as e:
            if e.args[0] != 1061:               # 1061 = index already exists, that's fine
                log.warning("schema statement skipped (%s): %s", e.args[0], body[:60])
            conn.rollback()

    cur.execute("SELECT COUNT(*) AS n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()")
    DB_FEATURES["triggers"] = cur.fetchone()["n"] >= 2

    # fill the analyte table and the colour chart the first time
    cur.execute("SELECT COUNT(*) AS n FROM analyte")
    if cur.fetchone()["n"] == 0:
        for order, (name, levels) in enumerate(CHART.items(), start=1):
            unit, mn, mx, desc = ANALYTE_INFO[name]
            cur.execute("INSERT INTO analyte (name, unit, pad_order, normal_min, normal_max, description) "
                        "VALUES (%s,%s,%s,%s,%s,%s)", (name, unit, order, mn, mx, desc))
            aid = cur.lastrowid
            for i, (label, val, (r, gg, b)) in enumerate(levels):
                cur.execute("INSERT INTO reference_color (analyte_id, level_index, level_label, numeric_value, r, g, b) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s)", (aid, i, label, val, r, gg, b))
        conn.commit()
    cur.execute("SELECT RELEASE_LOCK('uroscan_init')")
    cur.close()
    conn.close()
    log.info("DB ready. Triggers active: %s", DB_FEATURES["triggers"])
