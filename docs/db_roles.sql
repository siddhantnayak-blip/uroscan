-- =====================================================================
-- UroScan: DBMS demo queries for the viva
-- Run these in Aiven's query editor or MySQL Workbench.
-- =====================================================================

-- See all tables, the trigger and the procedure
SHOW TABLES;
SHOW TRIGGERS;
SHOW PROCEDURE STATUS WHERE Db = DATABASE();

-- View: one row per patient (used on the clinician's "Patients" page)
SELECT * FROM v_patient_summary;

-- Stored procedure: Glucose history of patient 1 (used for the graph)
CALL get_test_history(1, 'Glucose');

-- JOIN: full report 1 with test names
SELECT a.name, t.level_label, t.status
FROM test_result t JOIN analyte a ON a.analyte_id = t.analyte_id
WHERE t.report_id = 1
ORDER BY a.pad_order;

-- GROUP BY: how many times each test was flagged
SELECT a.name, COUNT(*) AS times_flagged
FROM test_result t JOIN analyte a ON a.analyte_id = t.analyte_id
WHERE t.status <> 'Normal'
GROUP BY a.name ORDER BY times_flagged DESC;

-- Index in use (look for idx_report_patient in the "key" column)
EXPLAIN SELECT * FROM test_report WHERE patient_id = 1 ORDER BY created_at;

-- Transaction demo: nothing is saved because of ROLLBACK
START TRANSACTION;
INSERT INTO test_report (patient_id) VALUES (1);
ROLLBACK;

-- Many-to-many: which clinician looks after which patient
SELECT c.full_name AS clinician, p.full_name AS patient, pc.assigned_at
FROM patient_clinician pc
JOIN users c ON c.user_id = pc.clinician_id
JOIN users p ON p.user_id = pc.patient_id
ORDER BY clinician, patient;

-- Audit log written by triggers (who did what, and when)
SELECT l.created_at, a.full_name AS done_by, l.action, l.details
FROM audit_log l LEFT JOIN users a ON a.user_id = l.actor_id
ORDER BY l.created_at DESC;

-- Clinician worklist: reports still waiting, urgent first
SELECT r.report_id, u.full_name, r.review_status, r.follow_up_date
FROM test_report r JOIN users u ON u.user_id = r.patient_id
WHERE r.review_status IN ('Pending', 'Urgent', 'Follow-up')
ORDER BY FIELD(r.review_status, 'Urgent', 'Pending', 'Follow-up'), r.created_at DESC;

-- Corrected readings (original camera value is kept)
SELECT t.report_id, a.name, t.original_label AS camera_read, t.level_label AS corrected_to, c.full_name AS corrected_by
FROM test_result t JOIN analyte a ON a.analyte_id = t.analyte_id JOIN users c ON c.user_id = t.corrected_by
WHERE t.original_label IS NOT NULL;

-- Users per role
SELECT role, COUNT(*) AS users, SUM(is_active = 0) AS disabled FROM users GROUP BY role;

-- GRANT / REVOKE demo: a read-only user for clinicians (change the password first)
CREATE USER IF NOT EXISTS 'uro_clinician'@'%' IDENTIFIED BY 'ChangeMe_Clinician1';
GRANT SELECT ON test_report TO 'uro_clinician'@'%';
GRANT SELECT ON test_result TO 'uro_clinician'@'%';
GRANT SELECT, INSERT ON clinician_note TO 'uro_clinician'@'%';
REVOKE INSERT ON clinician_note FROM 'uro_clinician'@'%';
SHOW GRANTS FOR 'uro_clinician'@'%';
