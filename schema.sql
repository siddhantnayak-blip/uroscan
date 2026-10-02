-- =====================================================================
-- UroScan database (MySQL 8)
-- Tables in 3NF/BCNF: every non-key column depends only on the primary key.
-- Statements are separated by lines containing only:  -- @@
-- =====================================================================

-- 1. USERS: one table for patients, clinicians and the admin (role column)
--    patients sign up themselves; clinicians are created by the admin; is_active = 0 blocks log-in
CREATE TABLE IF NOT EXISTS users (
  user_id       INT AUTO_INCREMENT PRIMARY KEY,
  full_name     VARCHAR(100) NOT NULL,
  email         VARCHAR(120) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  role          ENUM('patient','clinician','admin') NOT NULL DEFAULT 'patient',
  is_active     TINYINT(1) NOT NULL DEFAULT 1,
  dob           DATE NULL,
  gender        ENUM('Male','Female','Other') NULL,
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- @@
-- 2. ANALYTE: the 10 tests on the strip and their normal range
CREATE TABLE IF NOT EXISTS analyte (
  analyte_id  INT AUTO_INCREMENT PRIMARY KEY,
  name        VARCHAR(40) NOT NULL UNIQUE,
  unit        VARCHAR(20) NULL,
  pad_order   TINYINT NOT NULL UNIQUE,
  normal_min  DECIMAL(10,4) NOT NULL,
  normal_max  DECIMAL(10,4) NOT NULL,
  description VARCHAR(255) NULL,
  CONSTRAINT chk_pad_order CHECK (pad_order BETWEEN 1 AND 10)
);
-- @@
-- 3. REFERENCE_COLOR: the colour chart (used by the KNN colour matcher)
CREATE TABLE IF NOT EXISTS reference_color (
  ref_id        INT AUTO_INCREMENT PRIMARY KEY,
  analyte_id    INT NOT NULL,
  level_index   TINYINT NOT NULL,
  level_label   VARCHAR(30) NOT NULL,
  numeric_value DECIMAL(10,4) NOT NULL,
  r TINYINT UNSIGNED NOT NULL, g TINYINT UNSIGNED NOT NULL, b TINYINT UNSIGNED NOT NULL,
  UNIQUE KEY uq_ref (analyte_id, level_index),
  FOREIGN KEY (analyte_id) REFERENCES analyte(analyte_id) ON DELETE CASCADE
);
-- @@
-- 4. TEST_REPORT: one row per scan (+ the clinician's review status and follow-up date)
CREATE TABLE IF NOT EXISTS test_report (
  report_id      INT AUTO_INCREMENT PRIMARY KEY,
  patient_id     INT NOT NULL,
  created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  strip_image    MEDIUMBLOB NULL,
  ai_summary     TEXT NULL,
  abnormal_count TINYINT NOT NULL DEFAULT 0,
  review_status  ENUM('Pending','Reviewed','Follow-up','Urgent') NOT NULL DEFAULT 'Pending',
  follow_up_date DATE NULL,
  reviewed_by    INT NULL,
  reviewed_at    DATETIME NULL,
  FOREIGN KEY (patient_id)  REFERENCES users(user_id) ON DELETE CASCADE,
  FOREIGN KEY (reviewed_by) REFERENCES users(user_id) ON DELETE SET NULL
);
-- @@
-- 5. TEST_RESULT: weak entity -> 10 rows per report, key = (report_id, analyte_id)
CREATE TABLE IF NOT EXISTS test_result (
  report_id     INT NOT NULL,
  analyte_id    INT NOT NULL,
  level_label   VARCHAR(30) NOT NULL,
  level_index   TINYINT NOT NULL,
  numeric_value DECIMAL(10,4) NOT NULL,
  approx_value  DECIMAL(10,4) NULL,
  r TINYINT UNSIGNED NULL, g TINYINT UNSIGNED NULL, b TINYINT UNSIGNED NULL,
  status        ENUM('Normal','Trace','High','Low') NOT NULL DEFAULT 'Normal',
  original_label VARCHAR(30) NULL,          -- what the camera read, if a clinician corrected it
  corrected_by  INT NULL,
  corrected_at  DATETIME NULL,
  PRIMARY KEY (report_id, analyte_id),
  FOREIGN KEY (report_id)  REFERENCES test_report(report_id) ON DELETE CASCADE,
  FOREIGN KEY (analyte_id) REFERENCES analyte(analyte_id),
  FOREIGN KEY (corrected_by) REFERENCES users(user_id) ON DELETE SET NULL
);
-- @@
-- 6. CLINICIAN_NOTE: notes a clinician writes on a report
CREATE TABLE IF NOT EXISTS clinician_note (
  note_id      INT AUTO_INCREMENT PRIMARY KEY,
  report_id    INT NOT NULL,
  clinician_id INT NOT NULL,
  note         TEXT NOT NULL,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (report_id) REFERENCES test_report(report_id) ON DELETE CASCADE,
  FOREIGN KEY (clinician_id) REFERENCES users(user_id) ON DELETE CASCADE
);
-- @@
-- 6b. PATIENT_CLINICIAN: many-to-many - which clinician looks after which patient (set by the admin)
CREATE TABLE IF NOT EXISTS patient_clinician (
  patient_id   INT NOT NULL,
  clinician_id INT NOT NULL,
  assigned_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (patient_id, clinician_id),
  FOREIGN KEY (patient_id)   REFERENCES users(user_id) ON DELETE CASCADE,
  FOREIGN KEY (clinician_id) REFERENCES users(user_id) ON DELETE CASCADE
);
-- @@
-- 6c. AUDIT_LOG: filled by triggers - who did what and when (actor = @actor_id set by the app)
CREATE TABLE IF NOT EXISTS audit_log (
  log_id     BIGINT AUTO_INCREMENT PRIMARY KEY,
  actor_id   INT NULL,
  action     VARCHAR(40) NOT NULL,
  target_id  INT NULL,
  details    VARCHAR(255) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- @@
-- 7. Login safety tables (lock after 5 wrong passwords, password reset links)
CREATE TABLE IF NOT EXISTS login_attempt (
  attempt_id   BIGINT AUTO_INCREMENT PRIMARY KEY,
  email        VARCHAR(120) NOT NULL,
  success      TINYINT(1) NOT NULL,
  ip           VARCHAR(45) NULL,
  attempted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- @@
CREATE TABLE IF NOT EXISTS password_reset (
  token      CHAR(64) PRIMARY KEY,
  user_id    INT NOT NULL,
  expires_at DATETIME NOT NULL,
  used       TINYINT(1) NOT NULL DEFAULT 0,
  FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
-- @@
-- 8. INDEX: makes "all reports of a patient, newest first" fast
CREATE INDEX idx_report_patient ON test_report (patient_id, created_at);
-- @@
-- 9. VIEW: one line per patient for the clinician's and admin's lists
--    to_review    = reports still 'Pending'
--    urgent       = reports marked 'Urgent'
--    latest_flags = how many tests were flagged in the patient's newest report
CREATE OR REPLACE VIEW v_patient_summary AS
SELECT u.user_id AS patient_id, u.full_name, u.email, u.dob, u.gender,
       COUNT(r.report_id) AS total_reports,
       MAX(r.created_at)  AS last_scan,
       SUM(r.review_status = 'Pending') AS to_review,
       SUM(r.review_status = 'Urgent')  AS urgent,
       MIN(CASE WHEN r.review_status IN ('Follow-up','Urgent') THEN r.follow_up_date END) AS next_follow_up,
       (SELECT r2.abnormal_count FROM test_report r2 WHERE r2.patient_id = u.user_id
        ORDER BY r2.created_at DESC, r2.report_id DESC LIMIT 1) AS latest_flags
FROM users u
LEFT JOIN test_report r ON r.patient_id = u.user_id
WHERE u.role = 'patient'
GROUP BY u.user_id, u.full_name, u.email, u.dob, u.gender;
-- @@
-- 10. TRIGGER: decide Normal / Trace / High / Low automatically when a result is saved
DROP TRIGGER IF EXISTS trg_set_status;
-- @@
CREATE TRIGGER trg_set_status BEFORE INSERT ON test_result
FOR EACH ROW
BEGIN
  DECLARE mn DECIMAL(10,4);
  DECLARE mx DECIMAL(10,4);
  SELECT normal_min, normal_max INTO mn, mx FROM analyte WHERE analyte_id = NEW.analyte_id;
  IF NEW.numeric_value BETWEEN mn AND mx THEN
    SET NEW.status = 'Normal';
  ELSEIF NEW.level_label LIKE 'Trace%' THEN
    SET NEW.status = 'Trace';
  ELSEIF NEW.numeric_value < mn THEN
    SET NEW.status = 'Low';
  ELSE
    SET NEW.status = 'High';
  END IF;
END;
-- @@
-- 11. TRIGGER: keep the count of flagged tests on the report up to date
DROP TRIGGER IF EXISTS trg_count_abnormal;
-- @@
CREATE TRIGGER trg_count_abnormal AFTER INSERT ON test_result
FOR EACH ROW
BEGIN
  IF NEW.status <> 'Normal' THEN
    UPDATE test_report SET abnormal_count = abnormal_count + 1 WHERE report_id = NEW.report_id;
  END IF;
END;
-- @@
-- 11b. TRIGGER: if a clinician corrects a reading, work out its status again
DROP TRIGGER IF EXISTS trg_status_on_correct;
-- @@
CREATE TRIGGER trg_status_on_correct BEFORE UPDATE ON test_result
FOR EACH ROW
BEGIN
  DECLARE mn DECIMAL(10,4);
  DECLARE mx DECIMAL(10,4);
  IF NEW.level_index <> OLD.level_index THEN
    SELECT normal_min, normal_max INTO mn, mx FROM analyte WHERE analyte_id = NEW.analyte_id;
    IF NEW.numeric_value BETWEEN mn AND mx THEN SET NEW.status = 'Normal';
    ELSEIF NEW.level_label LIKE 'Trace%' THEN SET NEW.status = 'Trace';
    ELSEIF NEW.numeric_value < mn THEN SET NEW.status = 'Low';
    ELSE SET NEW.status = 'High';
    END IF;
  END IF;
END;
-- @@
-- 11c. AUDIT TRIGGERS: record admin actions in audit_log
DROP TRIGGER IF EXISTS trg_audit_user_add;
-- @@
CREATE TRIGGER trg_audit_user_add AFTER INSERT ON users
FOR EACH ROW
BEGIN
  IF NEW.role = 'clinician' THEN
    INSERT INTO audit_log (actor_id, action, target_id, details)
    VALUES (@actor_id, 'CREATE_CLINICIAN', NEW.user_id, CONCAT(NEW.full_name, ' <', NEW.email, '>'));
  END IF;
END;
-- @@
DROP TRIGGER IF EXISTS trg_audit_user_change;
-- @@
CREATE TRIGGER trg_audit_user_change AFTER UPDATE ON users
FOR EACH ROW
BEGIN
  IF NEW.is_active <> OLD.is_active THEN
    INSERT INTO audit_log (actor_id, action, target_id, details)
    VALUES (@actor_id, IF(NEW.is_active = 1, 'ENABLE_ACCOUNT', 'DISABLE_ACCOUNT'), NEW.user_id, NEW.full_name);
  END IF;
  IF NEW.password_hash <> OLD.password_hash AND @actor_id IS NOT NULL AND @actor_id <> NEW.user_id THEN
    INSERT INTO audit_log (actor_id, action, target_id, details)
    VALUES (@actor_id, 'RESET_PASSWORD', NEW.user_id, NEW.full_name);
  END IF;
END;
-- @@
DROP TRIGGER IF EXISTS trg_audit_assign;
-- @@
CREATE TRIGGER trg_audit_assign AFTER INSERT ON patient_clinician
FOR EACH ROW
INSERT INTO audit_log (actor_id, action, target_id, details)
VALUES (@actor_id, 'ASSIGN_PATIENT', NEW.clinician_id, (SELECT full_name FROM users WHERE user_id = NEW.patient_id));
-- @@
DROP TRIGGER IF EXISTS trg_audit_unassign;
-- @@
CREATE TRIGGER trg_audit_unassign AFTER DELETE ON patient_clinician
FOR EACH ROW
INSERT INTO audit_log (actor_id, action, target_id, details)
VALUES (@actor_id, 'UNASSIGN_PATIENT', OLD.clinician_id, (SELECT full_name FROM users WHERE user_id = OLD.patient_id));
-- @@
-- 12. STORED PROCEDURE: history of one test for one patient (used for the trend graph)
DROP PROCEDURE IF EXISTS get_test_history;
-- @@
CREATE PROCEDURE get_test_history(IN p_patient INT, IN p_analyte VARCHAR(40))
BEGIN
  SELECT r.report_id, r.created_at, t.level_index, t.level_label, t.status
  FROM test_report r
  JOIN test_result t ON t.report_id = r.report_id
  JOIN analyte a     ON a.analyte_id = t.analyte_id
  WHERE r.patient_id = p_patient AND a.name = p_analyte
  ORDER BY r.created_at;
END;
