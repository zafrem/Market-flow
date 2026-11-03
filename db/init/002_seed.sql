SET NAMES utf8mb4;

-- Phase 1 seed: fresh-produce starter catalog. Extend via the catalog admin tooling in later phases.
INSERT IGNORE INTO master_products (category_l, category_m, item_name, standard_unit) VALUES
  ('Fresh', 'Fruit',     'Apple',        '100g'),
  ('Fresh', 'Fruit',     'Pear',         '100g'),
  ('Fresh', 'Vegetable', 'Onion',        '100g'),
  ('Fresh', 'Vegetable', 'Potato',       '100g'),
  ('Fresh', 'Vegetable', 'Napa Cabbage', '100g'),
  ('Fresh', 'Vegetable', 'Radish',       '100g'),
  ('Fresh', 'Vegetable', 'Green Onion',  '100g'),
  ('Fresh', 'Vegetable', 'Garlic',       '100g');

-- Source-specific names (KAMIS / Garak report Korean item names) -> canonical items.
INSERT IGNORE INTO master_product_aliases (source_type, alias, std_item_id)
SELECT s.source_type, a.alias, p.std_item_id
FROM (SELECT 'KAMIS' AS source_type UNION ALL SELECT 'GARAK') s
JOIN (
  SELECT '사과' AS alias, 'Apple' AS item UNION ALL SELECT '배', 'Pear' UNION ALL SELECT '양파', 'Onion'
  UNION ALL SELECT '감자', 'Potato' UNION ALL SELECT '배추', 'Napa Cabbage' UNION ALL SELECT '무', 'Radish'
  UNION ALL SELECT '대파', 'Green Onion' UNION ALL SELECT '마늘', 'Garlic'
) a
JOIN master_products p ON p.item_name = a.item;
