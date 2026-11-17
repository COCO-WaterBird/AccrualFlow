
CREATE TABLE IF NOT EXISTS costcenter_mapping (
    legal_entity VARCHAR(20),
    employee_email_prefix VARCHAR(100),
    cost_center VARCHAR(30),
    department VARCHAR(100),
    role VARCHAR(50),
    manager VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS idx_employee_lookup
ON costcenter_mapping (
    legal_entity,
    employee_email_prefix
);
