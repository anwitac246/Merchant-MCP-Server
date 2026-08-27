"""
Quick CLI to exercise the recommender against the ingested dataset.

Usage:
    python -m scripts.test_recommender "wireless bluetooth speaker under 3000 with good battery life"
    python -m scripts.test_recommender               # runs a few built-in sample queries
    python -m scripts.test_recommender --interactive  # type your own queries in a loop
"""
from __future__ import annotations

import json
import sys

from services.recommender import recommend

# NOTE: the shipped dataset (data/raw/amz_uk_processed_data_recovered.csv) has
# no smartphone category -- it's 27 niche categories (audio, wearables,
# gaming accessories, motorbike parts, home/kitchen, lab gear, etc). A query
# like "phone under 3000, blue, samsung, good camera" will legitimately
# return few/no results on THIS dataset. The samples below are picked to
# actually hit data that exists, plus the original ask as a worked example.
SAMPLE_QUERIES = [
    "phone under 3000 which is blue and samsung and has good camera quality",
    "blue samsung wearable under 5000 with good battery life",
    "wireless bluetooth speaker under 3000",
    "black gaming headset with noise cancelling under 4000",
]


def run(query: str) -> None:
    result = recommend(query, limit=5)
    print(f"\n=== query: {query!r} ===")
    print(json.dumps(result, indent=2, default=str))


def main() -> None:
    args = sys.argv[1:]

    if args and args[0] == "--interactive":
        print("Type a buyer intent (empty line to quit).")
        while True:
            try:
                q = input("> ").strip()
            except EOFError:
                break
            if not q:
                break
            run(q)
        return

    if args:
        run(" ".join(args))
        return

    for q in SAMPLE_QUERIES:
        run(q)


if __name__ == "__main__":
    main()