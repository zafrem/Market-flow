-- Existing MySQL databases: promotion calendar. Fresh installs get it from 001_schema.sql.
CREATE TABLE IF NOT EXISTS promotions (
  promo_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(120) NOT NULL,
  category_l VARCHAR(50) NOT NULL DEFAULT '' COMMENT 'empty = every category',
  channel_name VARCHAR(50) NOT NULL DEFAULT '' COMMENT 'empty = every channel',
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  discount_pct DECIMAL(5,2) NULL,
  note VARCHAR(255) NULL,
  created_by VARCHAR(100) NOT NULL,
  KEY idx_promo_dates (start_date, end_date)
);
