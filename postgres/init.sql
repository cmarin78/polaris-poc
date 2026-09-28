-- polaris POC — Postgres init
--
-- Creates three databases (one per portal) on first boot:
--   pol_intranet   — employee directory, wiki links
--   pol_grafana    — Grafana metadata (Grafana owns its schema)
--   pol_customer   — multi-tenant data with RLS isolation
--
-- All created under the polaris_admin user with restrictive grants.

-- =============================================================================
-- pol_intranet: simple employee directory
-- =============================================================================
CREATE DATABASE pol_intranet;
\c pol_intranet

CREATE TABLE employees (
    id           SERIAL PRIMARY KEY,
    email        TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    department   TEXT NOT NULL,
    location     TEXT,
    role         TEXT NOT NULL,
    created_at   TIMESTAMP DEFAULT now()
);

CREATE INDEX idx_employees_department ON employees(department);
CREATE INDEX idx_employees_role ON employees(role);

GRANT ALL PRIVILEGES ON DATABASE pol_intranet TO polaris_admin;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO polaris_admin;

-- Seed data
INSERT INTO employees (email, display_name, department, location, role) VALUES
    ('ada@polaris.example',     'Ada Lovelace',     'Engineering',  'London',  'engineer'),
    ('linus@polaris.example',   'Linus Torvalds',   'Engineering',  'Portland','engineer'),
    ('grace@polaris.example',   'Grace Hopper',     'Operations',   'New York','ops'),
    ('barbara@polaris.example', 'Barbara Liskov',   'Operations',   'Boston',  'admin'),
    ('sara@polaris.example',    'Sara Northwind',   'Sales',        'Berlin',  'sales');


-- =============================================================================
-- pol_grafana: Grafana's metadata DB
-- Grafana 10.x owns its schema; we just give the polaris_admin user rights.
-- =============================================================================
CREATE DATABASE pol_grafana;
\c pol_grafana

GRANT ALL PRIVILEGES ON DATABASE pol_grafana TO polaris_admin;


-- =============================================================================
-- pol_customer: multi-tenant data with RLS isolation
-- =============================================================================
CREATE DATABASE pol_customer;
\c pol_customer

-- Dedicated non-superuser role for the customer portal. RLS does NOT
-- apply to SUPERUSER or BYPASSRLS roles, so we cannot reuse polaris_admin
-- (which is created as superuser by the official postgres image).
CREATE ROLE polaris_app NOSUPERUSER NOBYPASSRLS LOGIN PASSWORD 'polaris';

CREATE TABLE tenants (
    id   SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL  -- 'acme', 'brightside', 'partners'
);

CREATE TABLE customer_data (
    id          SERIAL PRIMARY KEY,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id),
    label       TEXT NOT NULL,
    value       TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT now()
);

-- Seed tenants
INSERT INTO tenants (name) VALUES
    ('acme'),
    ('brightside'),
    ('partners');

-- Seed sample data (one row per tenant so the walkthrough can show isolation)
INSERT INTO customer_data (tenant_id, label, value) VALUES
    (1, 'contract-id',       'ACME-2026-001'),
    (1, 'monthly-revenue',   '$ 4.2M'),
    (2, 'contract-id',       'BRIGHT-2026-007'),
    (2, 'monthly-revenue',   '$ 1.8M'),
    (3, 'partner-tier',      'gold');

-- =============================================================================
-- Row-Level Security (RLS): tenant isolation enforced at the DB level.
-- Even if the application code has a bug and forgets the WHERE clause,
-- Postgres will refuse to leak cross-tenant rows.
-- =============================================================================

ALTER TABLE customer_data ENABLE ROW LEVEL SECURITY;

-- Policy: a row is visible only when the current_setting matches the
-- tenant_id of the row. The application sets this with:
--   SET LOCAL app.current_tenant_id = '<int>';
-- Note: this policy uses the table owner (polaris_admin) for evaluation
-- because polaris_app does NOT own the table. polaris_app is NOT a
-- superuser, so the policy applies to it.
CREATE POLICY tenant_isolation ON customer_data
    USING (tenant_id = current_setting('app.current_tenant_id', true)::int)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::int);

-- By default RLS denies if the policy throws (e.g., current_setting is empty).
-- That's exactly what we want: no tenant set → no rows visible.

GRANT ALL PRIVILEGES ON DATABASE pol_customer TO polaris_admin;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO polaris_admin;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO polaris_admin;
-- Grant the application role only what it needs (least privilege).
GRANT CONNECT ON DATABASE pol_customer TO polaris_app;
GRANT USAGE ON SCHEMA public TO polaris_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO polaris_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO polaris_app;