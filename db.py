"""MySQL connection helpers + database initialisation."""
import os
import ssl
import logging
import pymysql
from pymysql.cursors import DictCursor
from flask import g

from reference import CHART, ANALYTE_INFO

log = logging.getLogger("uroscan.db")
DB_FEATURES = {"triggers": False, "procedure": False, "views": False}


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
    if os.environ.get("DB_SSL", "1") == "1":
        ctx = ssl.create_default_context()
        ca = os.environ.get("DB_CA_CERT")          # optional: paste Aiven CA certificate
        if ca:
            ctx.load_verify_locations(cadata=ca)
        else:                                       # encrypted, but without CA verification
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
    cur = get_db().cursor()
    cur.execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=None, commit=True):
    db = get_db()
    cur = db.cursor()
    cur.execute(sql, args)
    last = cur.lastrowid
    cur.close()
    if commit:
        db.commit()
    return last


def audit(user_id, action, details=""):
    try:
        execute("INSERT INTO audit_log (user_id, action, details) VALUES (%s,%s,%s)",
                (user_id, action, details[:255]))
    except Exception as e:  # never break a request because of logging
        log.warning("audit failed: %s", e)


def init_db():
    """Create tables, views, triggers, procedure (idempotent) and seed analytes."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT GET_LOCK('uroscan_init', 60)")   # only one worker builds the schema at a time
    try:  # needed to create triggers when binary logging is on (works only if the user may set it)
        cur.execute("SET GLOBAL log_bin_trust_function_creators = 1")
    except Exception:
        pass
    path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(path, encoding="utf-8") as fh:
        parts = [p.strip() for p in fh.read().split("-- @@")]
    for stmt in parts:
        body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
        if not body:
            continue
        try:
            cur.execute(body)
            conn.commit()
            if "CREATE TRIGGER" in body:
                DB_FEATURES["triggers"] = True
            if "CREATE PROCEDURE" in body:
                DB_FEATURES["procedure"] = True
            if "CREATE OR REPLACE VIEW" in body:
                DB_FEATURES["views"] = True
        except pymysql.err.OperationalError as e:
            code = e.args[0]
            if code == 1061:            # duplicate index -> already created
                continue
            log.warning("schema statement skipped (%s): %s", code, body[:60])
            conn.rollback()
        except Exception as e:
            log.warning("schema statement failed: %s -> %s", body[:60], e)
            conn.rollback()
    # detect triggers/procedure that already existed from a previous start
    cur.execute("SELECT COUNT(*) AS n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()")
    DB_FEATURES["triggers"] = cur.fetchone()["n"] >= 3
    cur.execute("SELECT COUNT(*) AS n FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = DATABASE() "
                "AND ROUTINE_NAME='get_patient_trend'")
    DB_FEATURES["procedure"] = cur.fetchone()["n"] == 1

    # seed analytes + reference colours
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
        cur.execute("INSERT IGNORE INTO laboratory (name, city) VALUES ('Somaiya Health Lab', 'Mumbai')")
        conn.commit()
    cur.execute("SELECT RELEASE_LOCK('uroscan_init')")
    cur.close()
    conn.close()
    log.info("DB ready. Features: %s", DB_FEATURES)
    return DB_FEATURES
