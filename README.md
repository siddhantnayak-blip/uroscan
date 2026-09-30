# UroScan: Smart Urine Strip Reader with a Healthcare Database

DBMS mini-project (S.Y. B.Tech) by **Siddhant Nayak (16014225060)** and **Anshul Parida (16014225063)**, K J Somaiya.

A patient uploads a photo of a 10-pad urine test strip. The app reads the colour of each pad, matches it to the
colour chart with **KNN**, saves the report in **MySQL**, and shows a simple report, an AI explanation and
trend graphs. A clinician can pick a patient, open their reports and add notes.

## Features
- **Login and sign-up** for two roles: patient and clinician. Passwords are hashed (with a strength meter and a "re-enter password" check), the app locks an email after 5 wrong tries for 15 minutes, and there's a forgot-password link.
- **Profile:** patients add their date of birth and gender; age and gender are shown on every report.
- **Scan:** take or upload a photo, or try one of the 6 sample strips. A progress animation shows each step.
- **Report:** each of the 10 tests in its own box with the value, approx number, normal range, status, the colour chart with the matched colour ringed, and the change since the previous scan (↑ / ↓ / =). It can be printed as a lab-style report.
- **AI summary** of every report and an **"Ask" chat**, both using the Google Gemini API. If there's no key, the app uses simple built-in rules instead.
- **Graphs:** a trend line for any test, a "normal vs flagged" bar chart per scan (GROUP BY) and a doughnut of the latest scan.
- **Clinician:** a dashboard with counts of new reports to review and patients with flagged results, a red dot next to those patients, then a patient's reports ("Needs review" until a note is added) and a report page to add notes.
- **Design:** teal theme, light / dark mode, phone-friendly layout with a sticky "New scan" button.

## How the strip is read (`vision.py`)
1. Find the coloured squares in the photo using OpenCV contours.
2. Fit a straight line through them with `np.polyfit`, then work out the positions of all 10 pads.
3. Take the average colour of the centre of each pad, corrected against the white paper around it.
4. **KNN:** compare that colour with every level on the chart using Euclidean distance in LAB colour space. The closest level is the result (e.g. "250 mg/dL"). If the colour lies between two neighbouring levels, the value is estimated in between (e.g. "about 180 mg/dL").

On 60 simulated strip photos, it found every strip and picked the exact chart level for about 93% of pads.

## Database (`schema.sql`)
| Concept | Where |
|---|---|
| Tables | `users`, `analyte`, `reference_color`, `test_report`, `test_result`, `clinician_note`, `login_attempt`, `password_reset` |
| Weak entity, composite key | `test_result (report_id, analyte_id)` |
| Constraints | PRIMARY KEY, FOREIGN KEY (ON DELETE CASCADE), UNIQUE, CHECK |
| Normalisation | 3NF/BCNF: test names and normal ranges are stored once in `analyte` |
| Triggers | `trg_set_status` (Normal/Trace/High/Low), `trg_count_abnormal` |
| View | `v_patient_summary` (the clinician's patient list: report counts, reports to review, latest flags) |
| Stored procedure | `get_test_history(patient, test)` (the graph) |
| Index | `idx_report_patient` |
| Transaction | `save_scan()` in `app.py`: the report and its 10 results are saved together, or rolled back |
| Demo queries | `docs/db_roles.sql` (JOIN, GROUP BY, EXPLAIN, ROLLBACK, GRANT/REVOKE) |

## Files
| File | What it does |
|---|---|
| `app.py` | Flask routes (pages) and the save transaction |
| `db.py` | MySQL connection; creates the tables at start-up |
| `vision.py` | Reads the strip (KNN colour matching) |
| `reference.py` | The colour chart and normal ranges |
| `ai.py` | Gemini AI summary and chat |
| `templates/` | HTML pages |
| `static/` | CSS (light + dark theme), JavaScript (graphs and chat) and sample strip photos |
| `tools/` | Makes simulated strip photos and measures accuracy |

## Running it
Hosted on **Render** (the Flask app) with **Aiven** (free MySQL).
Settings are passed in as environment variables: `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`,
`SECRET_KEY`, `GEMINI_API_KEY`, `COOKIE_SECURE=1`.

To run it locally: `pip install -r requirements.txt`, set the same variables, then `python app.py`.

> UroScan is a screening aid, not a diagnosis.
