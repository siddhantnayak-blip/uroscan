-- =====================================================================
-- UroScan database schema (MySQL 8)
-- Statements are separated by lines containing only:  -- @@
-- (so triggers / procedures with ; inside them can be run one by one)
-- Normalised to BCNF: every determinant is a candidate key.
-- =====================================================================

-- ---------- 1. USER and its IS-A specialisations (EER) ----------
CREATE TABLE IF NOT EXISTS users (
  user_id       INT AUTO_INCREMENT PRIMARY KEY,
  full_name     VARCHAR(100) NOT NULL,
  email         VARCHAR(120) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  role          ENUM('patient','labtech','clinician','admin') NOT NULL DEFAULT 'patient',
  is_active     TINYINT(1) NOT NULL DEFAULT 1,
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT chk_email CHECK (email LIKE '%_@_%._%')
);
-- @@
CREATE TABLE IF NOT EXISTS laboratory (
  lab_id   INT AUTO_INCREMENT PRIMARY KEY,
  name     VARCHAR(100) NOT NULL UNIQUE,
  city     VARCHAR(60)  NOT NULL
);
-- @@
CREATE TABLE IF NOT EXISTS patient (
  patient_id  INT PRIMARY KEY,
  dob         DATE NULL,
  gender      ENUM('Male','Female','Other') NULL,
  CONSTRAINT fk_patient_user FOREIGN KEY (patient_id) REFERENCES users(user_id) ON DELETE CASCADE
);
-- @@
CREATE TABLE IF NOT EXISTS clinician (
  clinician_id INT PRIMARY KEY,
  specialty    VARCHAR(80) NULL,
  lab_id       INT NULL,
  CONSTRAINT fk_clin_user FOREIGN KEY (clinician_id) REFERENCES users(user_id) ON DELETE CASCADE,
  CONSTRAINT fk_clin_lab  FOREIGN KEY (lab_id) REFERENCES laboratory(lab_id) ON DELETE SET NULL
);
-- @@
CREATE TABLE IF NOT EXISTS lab_technician (
  labtech_id INT PRIMARY KEY,
  lab_id     INT NULL,
  CONSTRAINT fk_lt_user FOREIGN KEY (labtech_id) REFERENCES users(user_id) ON DELETE CASCADE,
  CONSTRAINT fk_lt_lab  FOREIGN KEY (lab_id) REFERENCES laboratory(lab_id) ON DELETE SET NULL
);
-- @@
-- ---------- 2. Analytes and their reference colour chart ----------
CREATE TABLE IF NOT EXISTS analyte (
  analyte_id  INT AUTO_INCREMENT PRIMARY KEY,
  name        VARCHAR(40) NOT NULL UNIQUE,
  unit        VARCHAR(20) NULL,
  pad_order   TINYINT NOT NULL UNIQUE,
  normal_min  DECIMAL(10,4) NOT NULL,
  normal_max  DECIMAL(10,4) NOT NULL,
  description VARCHAR(255) NULL,
  CONSTRAINT chk_pad_order CHECK (pad_order BETWEEN 1 AND 10),
  CONSTRAINT chk_range CHECK (normal_min <= normal_max)
);
-- @@
CREATE TABLE IF NOT EXISTS reference_color (
  ref_id        INT AUTO_INCREMENT PRIMARY KEY,
  analyte_id    INT NOT NULL,
  level_index   TINYINT NOT NULL,
  level_label   VARCHAR(30) NOT NULL,
  numeric_value DECIMAL(10,4) NOT NULL,
  r TINYINT UNSIGNED NOT NULL, g TINYINT UNSIGNED NOT NULL, b TINYINT UNSIGNED NOT NULL,
  UNIQUE KEY uq_ref (analyte_id, level_index),
  CONSTRAINT fk_ref_analyte FOREIGN KEY (analyte_id) REFERENCES analyte(analyte_id) ON DELETE CASCADE
);
-- @@
-- ---------- 3. Sample -> strip -> report -> results ----------
CREATE TABLE IF NOT EXISTS urine_sample (
  sample_id    INT AUTO_INCREMENT PRIMARY KEY,
  patient_id   INT NOT NULL,
  collected_by INT NULL,
  collected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_sample_patient FOREIGN KEY (patient_id) REFERENCES patient(patient_id) ON DELETE CASCADE,
  CONSTRAINT fk_sample_by FOREIGN KEY (collected_by) REFERENCES users(user_id) ON DELETE SET NULL
);
-- @@
CREATE TABLE IF NOT EXISTS test_strip (
  strip_id     INT AUTO_INCREMENT PRIMARY KEY,
  sample_id    INT NOT NULL UNIQUE,
  brand        VARCHAR(60) NOT NULL DEFAULT 'Generic 10-parameter',
  image        MEDIUMBLOB NULL,
  annotated    MEDIUMBLOB NULL,
  quality_json JSON NULL,
  CONSTRAINT fk_strip_sample FOREIGN KEY (sample_id) REFERENCES urine_sample(sample_id) ON DELETE CASCADE
);
-- @@
CREATE TABLE IF NOT EXISTS test_report (
  report_id      INT AUTO_INCREMENT PRIMARY KEY,
  sample_id      INT NOT NULL UNIQUE,
  lab_id         INT NULL,
  created_by     INT NULL,
  created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ai_summary     TEXT NULL,
  abnormal_count TINYINT NOT NULL DEFAULT 0,
  is_demo        TINYINT(1) NOT NULL DEFAULT 0,
  reviewed_by    INT NULL,
  reviewed_at    DATETIME NULL,
  CONSTRAINT fk_rep_sample FOREIGN KEY (sample_id) REFERENCES urine_sample(sample_id) ON DELETE CASCADE,
  CONSTRAINT fk_rep_lab FOREIGN KEY (lab_id) REFERENCES laboratory(lab_id) ON DELETE SET NULL,
  CONSTRAINT fk_rep_by FOREIGN KEY (created_by) REFERENCES users(user_id) ON DELETE SET NULL,
  CONSTRAINT fk_rep_rev FOREIGN KEY (reviewed_by) REFERENCES users(user_id) ON DELETE SET NULL
);
-- @@
-- Weak entity: a result only exists inside a report -> composite primary key
CREATE TABLE IF NOT EXISTS diagnostic_result (
  report_id     INT NOT NULL,
  analyte_id    INT NOT NULL,
  level_index   TINYINT NOT NULL,
  level_label   VARCHAR(30) NOT NULL,
  numeric_value DECIMAL(10,4) NOT NULL,
  approx_value  DECIMAL(10,4) NULL,
  confidence    DECIMAL(4,3) NOT NULL DEFAULT 1.000,
  delta_e       DECIMAL(6,2) NULL,
  r TINYINT UNSIGNED NULL, g TINYINT UNSIGNED NULL, b TINYINT UNSIGNED NULL,
  status        ENUM('Normal','Trace','High','Low') NOT NULL DEFAULT 'Normal',
  PRIMARY KEY (report_id, analyte_id),
  CONSTRAINT fk_res_report  FOREIGN KEY (report_id)  REFERENCES test_report(report_id) ON DELETE CASCADE,
  CONSTRAINT fk_res_analyte FOREIGN KEY (analyte_id) REFERENCES analyte(analyte_id),
  CONSTRAINT chk_conf CHECK (confidence BETWEEN 0 AND 1)
);
-- @@
CREATE TABLE IF NOT EXISTS clinician_note (
  note_id      INT AUTO_INCREMENT PRIMARY KEY,
  report_id    INT NOT NULL,
  clinician_id INT NOT NULL,
  note         TEXT NOT NULL,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_note_rep FOREIGN KEY (report_id) REFERENCES test_report(report_id) ON DELETE CASCADE,
  CONSTRAINT fk_note_clin FOREIGN KEY (clinician_id) REFERENCES users(user_id) ON DELETE CASCADE
);
-- @@
CREATE TABLE IF NOT EXISTS alert (
  alert_id   INT AUTO_INCREMENT PRIMARY KEY,
  patient_id INT NOT NULL,
  report_id  INT NULL,
  analyte_id INT NULL,
  message    VARCHAR(255) NOT NULL,
  is_read    TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_alert_pat FOREIGN KEY (patient_id) REFERENCES patient(patient_id) ON DELETE CASCADE,
  CONSTRAINT fk_alert_rep FOREIGN KEY (report_id) REFERENCES test_report(report_id) ON DELETE CASCADE
);
-- @@
-- ---------- 4. Security tables ----------
CREATE TABLE IF NOT EXISTS audit_log (
  log_id     BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id    INT NULL,
  action     VARCHAR(40) NOT NULL,
  details    VARCHAR(255) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- @@
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
  CONSTRAINT fk_pr_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
-- @@
-- ---------- 5. Indexes (B+ tree) for fast history lookups ----------
CREATE INDEX idx_sample_patient_time ON urine_sample (patient_id, collected_at);
-- @@
CREATE INDEX idx_login_email_time ON login_attempt (email, attempted_at);
-- @@
CREATE INDEX idx_alert_patient ON alert (patient_id, is_read);
-- @@
-- ---------- 6. View: every patient's latest report ----------
CREATE OR REPLACE VIEW v_report_summary AS
SELECT r.report_id, s.patient_id, u.full_name AS patient_name, r.created_at,
       r.abnormal_count, r.reviewed_by, r.is_demo
FROM test_report r
JOIN urine_sample s ON s.sample_id = r.sample_id
JOIN users u        ON u.user_id   = s.patient_id;
-- @@
CREATE OR REPLACE VIEW v_latest_results AS
SELECT vs.patient_id, vs.report_id, vs.created_at, a.name AS analyte, a.pad_order,
       d.level_label, d.numeric_value, d.status
FROM v_report_summary vs
JOIN diagnostic_result d ON d.report_id = vs.report_id
JOIN analyte a ON a.analyte_id = d.analyte_id
WHERE vs.report_id = (SELECT v2.report_id FROM v_report_summary v2 WHERE v2.patient_id = vs.patient_id
                      ORDER BY v2.created_at DESC, v2.report_id DESC LIMIT 1);
-- @@
-- ---------- 7. Triggers ----------
DROP TRIGGER IF EXISTS trg_result_status;
-- @@
CREATE TRIGGER trg_result_status BEFORE INSERT ON diagnostic_result
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
DROP TRIGGER IF EXISTS trg_result_alert;
-- @@
CREATE TRIGGER trg_result_alert AFTER INSERT ON diagnostic_result
FOR EACH ROW
BEGIN
  DECLARE pid INT;
  DECLARE aname VARCHAR(40);
  IF NEW.status <> 'Normal' THEN
    UPDATE test_report SET abnormal_count = abnormal_count + 1 WHERE report_id = NEW.report_id;
  END IF;
  IF NEW.status IN ('High','Low') THEN
    SELECT s.patient_id INTO pid FROM test_report r JOIN urine_sample s ON s.sample_id = r.sample_id
      WHERE r.report_id = NEW.report_id;
    SELECT name INTO aname FROM analyte WHERE analyte_id = NEW.analyte_id;
    INSERT INTO alert (patient_id, report_id, analyte_id, message)
      VALUES (pid, NEW.report_id, NEW.analyte_id, CONCAT(aname, ' is ', NEW.status, ' (', NEW.level_label, ')'));
  END IF;
END;
-- @@
DROP TRIGGER IF EXISTS trg_report_audit;
-- @@
CREATE TRIGGER trg_report_audit AFTER INSERT ON test_report
FOR EACH ROW
BEGIN
  INSERT INTO audit_log (user_id, action, details)
  VALUES (NEW.created_by, 'REPORT_CREATED', CONCAT('report_id=', NEW.report_id));
END;
-- @@
-- ---------- 8. Stored procedure: one analyte's history for a patient ----------
DROP PROCEDURE IF EXISTS get_patient_trend;
-- @@
CREATE PROCEDURE get_patient_trend(IN p_patient INT, IN p_analyte VARCHAR(40))
BEGIN
  SELECT r.report_id, r.created_at, d.level_index, d.level_label, d.numeric_value, d.status
  FROM test_report r
  JOIN urine_sample s      ON s.sample_id = r.sample_id
  JOIN diagnostic_result d ON d.report_id = r.report_id
  JOIN analyte a           ON a.analyte_id = d.analyte_id
  WHERE s.patient_id = p_patient AND a.name = p_analyte
  ORDER BY r.created_at;
END;
