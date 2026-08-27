from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from db.models import Inventory, Product, Promotion
from db.session import get_session, init_db
from services.domain_vocab import BRANDS, COLORS, FEATURE_PHRASES

RAW_CSV = Path(__file__).resolve().parent.parent / "data" / "raw" / "amazon_uk_products.csv"
CHUNK_SIZE = 200_000
SAMPLE_PER_CATEGORY = 50  # products kept per category -> ~1,350 products across 27 categories
GBP_TO_INR = 105.0

LOW_VELOCITY_QUANTILE = 0.25

_COLOR_PATTERNS = sorted(COLORS, key=len, reverse=True)
_BRAND_RE = re.compile(r"\b(" + "|".join(re.escape(b) for b in BRANDS) + r")\b", re.IGNORECASE)


def _extract_brand(title: str) -> str:
    m = _BRAND_RE.search(title or "")
    return m.group(1).title() if m else "Unbranded"


def _extract_color(title: str) -> str:
    t = (title or "").lower()
    for color in _COLOR_PATTERNS:
        if color in t:
            return color.title()
    return "Standard"


def _extract_features(title: str) -> list[str]:
    t = (title or "").lower()
    found = []
    for phrase, tag in FEATURE_PHRASES.items():
        if phrase in t and tag not in found:
            found.append(tag)
    return found


def _deterministic_stock(asin: str) -> int:
    h = int(hashlib.sha1(asin.encode()).hexdigest(), 16)
    return 3 + (h % 148)  # 3 .. 150


def _deterministic_discount(asin: str) -> float:
    h = int(hashlib.sha1((asin + "promo").encode()).hexdigest(), 16)
    return round(10 + (h % 16), 1)  # 10.0 .. 25.0


def load_filtered(csv_path: Path = RAW_CSV) -> pd.DataFrame:
    kept_frames: list[pd.DataFrame] = []

    for chunk in pd.read_csv(
        csv_path,
        usecols=["asin", "title", "stars", "reviews", "price", "isBestSeller", "boughtInLastMonth", "categoryName"],
        chunksize=CHUNK_SIZE,
    ):
        chunk = chunk[(chunk["price"] > 0) & (chunk["stars"] > 0)]
        chunk = chunk.dropna(subset=["categoryName", "title"])
        if len(chunk):
            kept_frames.append(chunk)

    if not kept_frames:
        raise ValueError("No rows survived the defensive filter -- check the CSV's price/stars columns.")

    df = pd.concat(kept_frames, ignore_index=True)
    df = df.drop_duplicates(subset="asin")

    sampled = [
        g.sample(n=min(SAMPLE_PER_CATEGORY, len(g)), random_state=42)
        for _, g in df.groupby("categoryName")
    ]
    df = pd.concat(sampled, ignore_index=True)

    df["brand"] = df["title"].map(_extract_brand)
    df["color"] = df["title"].map(_extract_color)
    df["features"] = df["title"].map(_extract_features)
    df["price_inr"] = (df["price"] * GBP_TO_INR / 10).round() * 10

    df["velocity_percentile"] = df.groupby("categoryName")["boughtInLastMonth"].rank(pct=True)
    df["is_low_velocity"] = df["velocity_percentile"] <= LOW_VELOCITY_QUANTILE

    return df


def _derive_tags(row: pd.Series) -> str:
    price_tier = (
        "budget" if row["price_percentile"] < 0.33
        else "mid-range" if row["price_percentile"] < 0.66
        else "premium"
    )
    tags = [
        row["categoryName"].lower().replace(" ", "-").replace(",", ""),
        row["brand"].lower(),
        row["color"].lower(),
        price_tier,
        *row["features"],
    ]
    if row["isBestSeller"]:
        tags.append("bestseller")
    if row["stars"] >= 4.3:
        tags.append("top-rated")
    return ",".join(tags)


def ingest(csv_path: Path = RAW_CSV, drop_first: bool = True) -> dict:
    init_db(drop_first=drop_first)
    df = load_filtered(csv_path)
    df["price_percentile"] = df.groupby("categoryName")["price_inr"].rank(pct=True)

    session = get_session()
    now = datetime.now(timezone.utc)
    n_products = n_promotions = 0

    try:
        for _, row in df.iterrows():
            title = str(row["title"])[:300]
            product = Product(
                sku=str(row["asin"]),
                source_row_id=str(row["asin"]),
                name=str(row["categoryName"]),
                category=str(row["categoryName"]),
                description=title,
                price=float(row["price_inr"]),
                currency="INR",
                warranty_period_years=1,
                dimensions_cm="N/A",
                manufacturing_date="N/A",
                expiration_date="N/A",
                tags=_derive_tags(row),
                variant=f"{row['color']}/Standard",
                color=row["color"],
                size="Standard",
                rating=max(1, min(5, round(row["stars"]))),
            )
            session.add(product)
            session.flush()

            session.add(Inventory(product_id=product.id, stock_quantity=_deterministic_stock(str(row["asin"]))))
            n_products += 1

            if row["is_low_velocity"]:
                session.add(
                    Promotion(
                        product_id=product.id,
                        discount_percent=_deterministic_discount(str(row["asin"])),
                        reason="Low sales velocity clearance",
                        valid_from=now,
                        valid_until=now + timedelta(days=30),
                        is_active=True,
                    )
                )
                n_promotions += 1

        session.commit()
    finally:
        session.close()

    return {
        "products_ingested": n_products,
        "promotions_created": n_promotions,
        "categories": sorted(df["categoryName"].unique().tolist()),
    }


if __name__ == "__main__":
    print(ingest())