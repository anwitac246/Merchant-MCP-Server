"""
Vocab used for (a) extracting structured attributes out of free-text product
titles during ingestion, and (b) parsing buyer intent in the recommender.

Tuned against the 27 categories actually present in
data/raw/amz_uk_processed_data_recovered.csv (audio, wearables, gaming
accessories, motorbike parts, home/kitchen, lab equipment, instruments,
lighting, storage) -- NOT a phone/smartphone catalog. Extend as needed.
"""
from __future__ import annotations

# Order doesn't matter here -- normalize.py sorts by length before regex-matching
# so multi-word brands (e.g. "Western Digital") win over partial overlaps.
BRANDS = [
    "Samsung", "Apple", "Sony", "Anker", "JBL", "Bose", "LG", "Philips",
    "Philips Hue", "Logitech", "Razer", "Corsair", "SteelSeries", "HyperX",
    "Sennheiser", "Yamaha", "Fender", "Ibanez", "Roland", "Casio", "Boss",
    "Shure", "Rode", "Amazon", "Echo", "Google", "Bosch", "Dyson",
    "De'Longhi", "Nespresso", "Krups", "Breville", "Ninja", "Duracell",
    "Energizer", "Osram", "Ring", "Nest", "TP-Link", "Netgear", "Michelin",
    "Castrol", "Motul", "Oxford", "RST", "Alpinestars", "Shoei", "AGV",
    "HJC", "Garmin", "Fitbit", "Xiaomi", "Huawei", "OnePlus", "Nokia",
    "Motorola", "Panasonic", "Pioneer", "Kenwood", "JVC", "Toshiba",
    "Western Digital", "Seagate", "SanDisk", "Kingston", "Crucial", "ASUS",
    "Acer", "HP", "Dell", "Lenovo", "Microsoft", "Nintendo", "PlayStation",
    "Xbox", "Belkin", "Aukey", "Ugreen", "Baseus", "Tefal", "Russell Hobbs",
    "Morphy Richards", "Karcher", "Vax", "Shark", "Hoover", "Black+Decker",
    "Makita", "DeWalt", "Bahco", "Stanley", "3M",
]

COLORS = [
    "black", "white", "blue", "navy", "midnight blue", "deep sea blue",
    "sky blue", "red", "green", "yellow", "grey", "gray", "space grey",
    "silver", "gold", "rose gold", "pink", "purple", "orange", "brown",
    "beige", "teal", "turquoise", "maroon", "charcoal", "clear", "multicolor",
]

# phrase (lowercase substring, matched against the title) -> canonical feature tag
FEATURE_PHRASES = {
    "noise cancelling": "noise-cancelling",
    "noise-cancelling": "noise-cancelling",
    "active noise cancellation": "noise-cancelling",
    "waterproof": "waterproof",
    "water resistant": "water-resistant",
    "water-resistant": "water-resistant",
    "wireless": "wireless",
    "bluetooth": "bluetooth",
    "fast charging": "fast-charging",
    "quick charge": "fast-charging",
    "long battery life": "long-battery-life",
    "hd": "hd",
    "4k": "4k",
    "full hd": "hd",
    "touchscreen": "touchscreen",
    "rechargeable": "rechargeable",
    "portable": "portable",
    "camera": "camera",
    "hd camera": "camera",
    "gaming": "gaming",
    "lightweight": "lightweight",
    "durable": "durable",
    "heavy duty": "durable",
    "eco friendly": "eco-friendly",
    "voice control": "voice-control",
    "smart": "smart",
    "stereo": "stereo",
    "surround sound": "surround-sound",
    "rgb": "rgb",
    "led": "led",
    "adjustable": "adjustable",
    "foldable": "foldable",
    "compact": "compact",
    "wifi": "wifi",
    "wi-fi": "wifi",
    "usb-c": "usb-c",
    "type-c": "usb-c",
}