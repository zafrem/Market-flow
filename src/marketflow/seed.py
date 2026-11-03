"""Starter catalog (mirrors db/init/002_seed.sql; a test keeps the two in sync)."""
ITEMS: list[tuple[str, str, str, str]] = [  # (category_l, category_m, item_name, standard_unit)
    ("Fresh", "Fruit", "Apple", "100g"), ("Fresh", "Fruit", "Pear", "100g"),
    ("Fresh", "Vegetable", "Onion", "100g"), ("Fresh", "Vegetable", "Potato", "100g"),
    ("Fresh", "Vegetable", "Napa Cabbage", "100g"), ("Fresh", "Vegetable", "Radish", "100g"),
    ("Fresh", "Vegetable", "Green Onion", "100g"), ("Fresh", "Vegetable", "Garlic", "100g"),
]
ALIASES: dict[str, str] = {  # Korean source name -> item_name
    "사과": "Apple", "배": "Pear", "양파": "Onion", "감자": "Potato",
    "배추": "Napa Cabbage", "무": "Radish", "대파": "Green Onion", "마늘": "Garlic",
}
SOURCES = ("KAMIS", "GARAK", "RETAIL")
