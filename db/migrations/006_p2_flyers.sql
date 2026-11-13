-- Existing MySQL databases: flyer review (items read from images wait for a person). Fresh installs get it from 001_schema.sql.
CREATE TABLE IF NOT EXISTS flyers (
  flyer_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  source_name VARCHAR(120) NOT NULL,
  image_path VARCHAR(255) NOT NULL COMMENT 'relative to the inbox root',
  week_start DATE NOT NULL,
  status VARCHAR(10) NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING until every item is decided, then REVIEWED',
  created_at DATETIME NOT NULL,
  reviewed_by VARCHAR(100) NULL,
  reviewed_at DATETIME NULL,
  KEY idx_flyer_status (status)
);

CREATE TABLE IF NOT EXISTS flyer_items (
  item_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  flyer_id INT NOT NULL,
  name VARCHAR(80) NOT NULL,
  price INT NULL,
  confidence DECIMAL(4,3) NULL COMMENT 'the reader''s own estimate, 0 to 1; NULL = not given',
  category_l VARCHAR(50) NOT NULL DEFAULT '',
  status VARCHAR(10) NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING, APPROVED or REJECTED; only APPROVED reaches analysis',
  KEY idx_item_flyer (flyer_id)
);
