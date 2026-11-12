-- Existing MySQL databases: synonym dictionary for market-intelligence keywords (SRS F-08-2.6). Fresh installs get it from 001_schema.sql.
CREATE TABLE IF NOT EXISTS mi_synonyms (
  alias VARCHAR(100) NOT NULL PRIMARY KEY,
  canonical VARCHAR(100) NOT NULL,
  updated_by VARCHAR(100) NOT NULL DEFAULT '',
  updated_at DATETIME NULL
);
