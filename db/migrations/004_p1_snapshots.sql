-- Existing MySQL databases: revision history of inventory/cost loads for point-in-time reads (SRS F-08-1.4).
-- Fresh installs get it from 001_schema.sql. History starts at the first load after this migration.
CREATE TABLE IF NOT EXISTS inventory_revisions (
  rev_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  ingested_at DATETIME NOT NULL COMMENT 'when the system learned this version (occurred time is recorded_date)',
  recorded_date DATE NOT NULL,
  internal_sku VARCHAR(50) NOT NULL,
  channel_name VARCHAR(50) NOT NULL,
  std_item_id INT NOT NULL,
  purchase_cost DECIMAL(10,2) NOT NULL,
  fixed_cost DECIMAL(10,2) NOT NULL,
  logistics_cost DECIMAL(10,2) NOT NULL,
  channel_fee_rate DECIMAL(5,2) NOT NULL,
  promo_cost DECIMAL(10,2) NOT NULL DEFAULT 0,
  disposal_cost DECIMAL(10,2) NULL,
  vat_inclusive TINYINT(1) NOT NULL DEFAULT 1,
  pack_std_units DECIMAL(10,4) NOT NULL DEFAULT 1,
  current_selling_price INT NOT NULL,
  stock_qty INT NOT NULL,
  daily_sales_qty INT NOT NULL,
  min_price_floor INT NOT NULL,
  lifecycle_metric JSON NOT NULL,
  KEY idx_rev (recorded_date, internal_sku, channel_name, ingested_at)
);
