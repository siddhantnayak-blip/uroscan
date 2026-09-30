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

-- GRANT / REVOKE demo: a read-only user for clinicians (change the password first)
CREATE USER IF NOT EXISTS 'uro_clinician'@'%' IDENTIFIED BY 'ChangeMe_Clinician1';
GRANT SELECT ON test_report TO 'uro_clinician'@'%';
GRANT SELECT ON test_result TO 'uro_clinician'@'%';
GRANT SELECT, INSERT ON clinician_note TO 'uro_clinician'@'%';
REVOKE INSERT ON clinician_note FROM 'uro_clinician'@'%';
SHOW GRANTS FOR 'uro_clinician'@'%';
