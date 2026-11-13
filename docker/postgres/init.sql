-- Development-only credentials. Never reuse these outside a local environment.
-- Table ownership and runtime access are intentionally separated:
--   migrator: owns the schema, runs migrations, and can create test databases.
--   app_user: runtime only; RLS policies apply to this non-owner role.
CREATE ROLE migrator LOGIN PASSWORD 'migrator' CREATEDB;
CREATE ROLE app_user LOGIN PASSWORD 'app_user' NOSUPERUSER NOBYPASSRLS;

CREATE DATABASE compliance OWNER migrator;

\connect compliance

ALTER SCHEMA public OWNER TO migrator;
GRANT CONNECT ON DATABASE compliance TO app_user;
GRANT USAGE ON SCHEMA public TO app_user;

ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_user;
