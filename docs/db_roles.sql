-- =====================================================================
-- UroScan: database-level access control (GRANT / REVOKE)
-- Run manually as the admin user (e.g. in Aiven's query editor or MySQL Workbench)
-- to show role separation at the DBMS level during the viva.
-- Change the passwords before running.
-- =====================================================================

-- Patient app role: can read results, cannot change reference data
CREATE USER IF NOT EXISTS 'uro_patient'@'%' IDENTIFIED BY 'ChangeMe_Patient1';
GRANT SELECT ON defaultdb.v_report_summary TO 'uro_patient'@'%';
GRANT SELECT ON defaultdb.v_latest_results TO 'uro_patient'@'%';
GRANT EXECUTE ON PROCEDURE defaultdb.get_patient_trend TO 'uro_patient'@'%';

-- Lab technician: can insert new samples / strips / reports / results
CREATE USER IF NOT EXISTS 'uro_labtech'@'%' IDENTIFIED BY 'ChangeMe_Labtech1';
GRANT SELECT ON defaultdb.analyte TO 'uro_labtech'@'%';
GRANT SELECT ON defaultdb.reference_color TO 'uro_labtech'@'%';
GRANT SELECT, INSERT ON defaultdb.urine_sample TO 'uro_labtech'@'%';
GRANT SELECT, INSERT ON defaultdb.test_strip TO 'uro_labtech'@'%';
GRANT SELECT, INSERT ON defaultdb.test_report TO 'uro_labtech'@'%';
GRANT SELECT, INSERT ON defaultdb.diagnostic_result TO 'uro_labtech'@'%';

-- Clinician: read everything clinical, write notes and review status
CREATE USER IF NOT EXISTS 'uro_clinician'@'%' IDENTIFIED BY 'ChangeMe_Clinic1';
GRANT SELECT ON defaultdb.* TO 'uro_clinician'@'%';
GRANT INSERT ON defaultdb.clinician_note TO 'uro_clinician'@'%';
GRANT UPDATE (reviewed_by, reviewed_at) ON defaultdb.test_report TO 'uro_clinician'@'%';

-- Clinicians must NOT read password hashes or the login log
REVOKE SELECT ON defaultdb.* FROM 'uro_clinician'@'%';
GRANT SELECT ON defaultdb.patient TO 'uro_clinician'@'%';
GRANT SELECT ON defaultdb.urine_sample TO 'uro_clinician'@'%';
GRANT SELECT ON defaultdb.test_report TO 'uro_clinician'@'%';
GRANT SELECT ON defaultdb.diagnostic_result TO 'uro_clinician'@'%';
GRANT SELECT ON defaultdb.analyte TO 'uro_clinician'@'%';
GRANT SELECT ON defaultdb.clinician_note TO 'uro_clinician'@'%';
GRANT SELECT (user_id, full_name, email, role) ON defaultdb.users TO 'uro_clinician'@'%';

SHOW GRANTS FOR 'uro_clinician'@'%';

-- Useful demo queries -------------------------------------------------
-- CALL get_patient_trend(1, 'Glucose');
-- SELECT * FROM v_latest_results WHERE patient_id = 1;
-- SHOW TRIGGERS;
-- EXPLAIN SELECT * FROM urine_sample WHERE patient_id = 1 ORDER BY collected_at;   -- uses idx_sample_patient_time
