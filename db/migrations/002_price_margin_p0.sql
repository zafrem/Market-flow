-- Existing MySQL databases: new columns for the price/margin P0 work (SRS F-08). Fresh installs get them from 001_schema.sql.
-- Run once: mysql marketflow < db/migrations/002_price_margin_p0.sql
ALTER TABLE ext_retail_prices
  ADD COLUMN coupon_discount INT NOT NULL DEFAULT 0 COMMENT 'public coupon, KRW off the shown price',
  ADD COLUMN shipping_fee INT NOT NULL DEFAULT 0 COMMENT 'delivery fee a buyer pays for this listing',
  ADD COLUMN bonus_qty DECIMAL(6,2) NOT NULL DEFAULT 0 COMMENT 'free units per paid unit (1+1 = 1)',
  ADD COLUMN effective_unit_price DECIMAL(10,2) NULL COMMENT 'unit price after coupon, shipping and bonus units';
ALTER TABLE int_operations_inventory
  ADD COLUMN disposal_cost DECIMAL(10,2) NULL COMMENT 'per-unit write-off cost; NULL = use the category disposal rate';
-- product_channel_map was created empty and unused in earlier releases.
ALTER TABLE product_channel_map
  ADD COLUMN match_grade VARCHAR(10) NOT NULL DEFAULT 'SAME' COMMENT 'SAME | SIMILAR | SUBSTITUTE',
  MODIFY COLUMN reviewed_by VARCHAR(100) NULL;
