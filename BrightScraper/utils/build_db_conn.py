from __future__ import annotations

import os
from functools import lru_cache
from typing import Any
import logging

try:
    import pymongo
except Exception:  # pragma: no cover
    pymongo = None  # type: ignore[assignment]


DEFAULT_MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/phyo")
DEFAULT_DB_NAME = os.getenv("MONGO_DB_NAME", "mydatabase")


@lru_cache(maxsize=1)
def build_db_conn() -> Any | None:
    if pymongo is None:
        return None

    try:
        client = pymongo.MongoClient(DEFAULT_MONGO_URI, serverSelectionTimeoutMS=2000)
        client.admin.command("ping")
        logging.info("Successfully connected to MongoDB.")
        return client[DEFAULT_DB_NAME]
    except Exception:
        logging.error("Failed to connect to MongoDB at %s", DEFAULT_MONGO_URI)
        return None


if __name__ == "__main__":
    db = build_db_conn()
    print(db if db is not None else "Database connection failed.")
