# UroScan: Smart Urine Strip Reader with a Healthcare Database

DBMS mini-project by **Siddhant Nayak (16014225060)** and **Anshul Parida (16014225063)**, K J Somaiya.

A web app that reads a 10-pad urine test strip from a phone photo, matches every pad's colour to the
reference chart, stores the result in a normalised MySQL database and shows trends on a dashboard.

## Features
- **Accounts:** sign-up, login, "remember me", forgot/reset password, change password.
  Roles: patient, lab technician, clinician and admin. Passwords are hashed (scrypt).
  The app locks an email out after 5 failed logins in 15 minutes and uses CSRF tokens on every form.
- **Scan:** camera capture with a guide box, photo upload or drag-and-drop, a 60-second reading timer, and 6 sample strips.
- **Computer vision** (`vision.py`, OpenCV):
  1. Flat-field white balance: a max-filter estimates the local white, which removes shadows, colour cast and vignette.
  2. Pad detection: finds colour blobs, fits a line through them (RANSAC), straightens the strip and fits the 10-pad grid.
  3. Glare check and blur check.
- **AI colour matching:** each pad is converted to CIELAB and matched with KNN (k=2) using the CIEDE2000 distance.
  The output is the level, an approximate value interpolated between the two nearest levels, and a confidence score.
- **Dashboard:**
  - health score and one tile per test with a trend arrow
  - trend chart with the normal range shaded
  - normal vs flagged per scan, a radar chart of latest vs average, and flags by test
  - alerts, including "abnormal 3 scans in a row", and a forecast of when a rising value may leave the normal range
- **AI (Gemini):** a plain-English summary of every report and an "Ask UroScan" chat that answers from the patient's own data.
  Only test values are sent, never names. There's a rule-based fallback if no key is set.
- **Clinician:** patients sorted by risk, full dashboard per patient, notes and "reviewed" status.
- **Admin:** stats, users (change role, enable/disable), DB feature status, audit log and the reference colour chart.

## DBMS concepts used (see `schema.sql`)
| Concept | Where |
|---|---|
| EER specialisation | `users` → `patient`, `clinician`, `lab_technician` |
| Weak entity, composite primary key | `diagnostic_result (report_id, analyte_id)` |
| Constraints | PK, FK (CASCADE / SET NULL), UNIQUE, CHECK |
| Normalisation to BCNF | analyte facts stored once in `analyte`; colours in `reference_color` |
| Triggers | `trg_result_status` (sets Normal/Trace/High/Low), `trg_result_alert` (alerts and abnormal count), `trg_report_audit` |
| Views | `v_report_summary`, `v_latest_results` |
| Stored procedure | `get_patient_trend(patient, analyte)` |
| Indexes | `idx_sample_patient_time`, `idx_login_email_time`, `idx_alert_patient` |
| Transactions | `save_scan()` in `app.py`: START TRANSACTION, then SAVEPOINT, then COMMIT, or ROLLBACK on any error |
| GRANT / REVOKE | `docs/db_roles.sql` |

If the MySQL server doesn't allow triggers, the app runs the same logic in Python inside the same transaction.
The admin page shows which features are active.

## Environment variables
| Name | Example |
|---|---|
| `DB_HOST` | `uroscan-db-uroscan.a.aivencloud.com` |
| `DB_PORT` | `28874` |
| `DB_USER` | `avnadmin` |
| `DB_PASSWORD` | *(from Aiven)* |
| `DB_NAME` | `defaultdb` |
| `SECRET_KEY` | any long random text |
| `ADMIN_EMAIL` | the email that becomes admin when it signs up |
| `GEMINI_API_KEY` | *(optional)* Google AI Studio key |
| `COOKIE_SECURE` | `1` on HTTPS hosting |
| `PYTHON_VERSION` | `3.12.7` (Render) |

## Deploy on Render
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn -w 2 --timeout 90 app:app`
- **Health check path:** `/health`, which is also used by UptimeRobot to keep the site awake

## Run locally
```
pip install -r requirements.txt
set DB_HOST=... (and the other variables)
python app.py        # opens on http://localhost:5000
```

## Testing the colour reader
```
python tools/generate_strips.py --n 200 --out dataset   # simulated strip photos with known answers
python tools/evaluate.py dataset                          # prints accuracy per test
```
On 60 simulated photos (random lighting, glare, shadow, blur and tilt), the reader found the strip every time.
It picked the exact chart level for **92.7%** of pads and was within one level for **99.8%**.
About 20% of the simulated pads are deliberately halfway between two levels.

> UroScan is a screening aid, not a diagnosis. Reference colours are approximate and should be calibrated
> against the chart printed on the actual strip bottle.
