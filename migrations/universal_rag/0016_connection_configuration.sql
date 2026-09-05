ALTER TABLE provider_connections ADD COLUMN configuration_version INTEGER NOT NULL DEFAULT 1 CHECK(configuration_version > 0);
ALTER TABLE provider_validation_runs ADD COLUMN configuration_version INTEGER NOT NULL DEFAULT 1 CHECK(configuration_version > 0);
ALTER TABLE provider_validation_runs ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '{}';
