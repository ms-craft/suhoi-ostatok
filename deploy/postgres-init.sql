\getenv migration_password POSTGRES_MIGRATION_PASSWORD
\getenv runtime_password POSTGRES_RUNTIME_PASSWORD

CREATE ROLE stopkadr_migrator LOGIN PASSWORD :'migration_password';
CREATE ROLE stopkadr_runtime LOGIN PASSWORD :'runtime_password';
ALTER DATABASE stopkadr OWNER TO stopkadr_migrator;
REVOKE ALL ON DATABASE stopkadr FROM PUBLIC;
GRANT CONNECT ON DATABASE stopkadr TO stopkadr_runtime;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO stopkadr_runtime;

ALTER DEFAULT PRIVILEGES FOR ROLE stopkadr_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO stopkadr_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE stopkadr_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO stopkadr_runtime;
