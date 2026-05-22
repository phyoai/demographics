from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

try:
    from pymongo import MongoClient, ReplaceOne
    from pymongo.collection import Collection
    from pymongo.database import Database
    from pymongo.errors import BulkWriteError, PyMongoError
    PYMONGO_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover
    MongoClient = None  # type: ignore[assignment]
    ReplaceOne = None  # type: ignore[assignment]
    Collection = Any  # type: ignore[assignment]
    Database = Any  # type: ignore[assignment]
    BulkWriteError = Exception  # type: ignore[assignment]
    PyMongoError = Exception  # type: ignore[assignment]
    PYMONGO_IMPORT_ERROR = exc


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent

load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(REPO_ROOT / ".env")
load_dotenv(REPO_ROOT / ".env.production")

DEFAULT_SOURCE_URI = os.getenv("SOURCE_MONGO_URI", "mongodb://localhost:27017")
DEFAULT_SOURCE_DB = os.getenv("SOURCE_MONGO_DB_NAME", "instagpy")
DEFAULT_TARGET_URI = (
    os.getenv("TARGET_MONGO_URI")
    or os.getenv("MONGODB_URI")
    or os.getenv("MONGO_URI")
    or ""
)
DEFAULT_TARGET_DB = (
    os.getenv("TARGET_MONGO_DB_NAME")
    or os.getenv("MONGO_DB_NAME")
    or DEFAULT_SOURCE_DB
)
DEFAULT_BATCH_SIZE = max(int(os.getenv("MONGO_TRANSFER_BATCH_SIZE", "1000")), 1)


@dataclass(slots=True)
class CollectionTransferResult:
    name: str
    documents_seen: int = 0
    documents_written: int = 0
    failed: bool = False
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transfer all collections from a local MongoDB database to another MongoDB server."
    )
    parser.add_argument(
        "--source-uri",
        default=DEFAULT_SOURCE_URI,
        help=f"Source MongoDB URI (default: {DEFAULT_SOURCE_URI})",
    )
    parser.add_argument(
        "--source-db",
        default=DEFAULT_SOURCE_DB,
        help=f"Source MongoDB database name (default: {DEFAULT_SOURCE_DB})",
    )
    parser.add_argument(
        "--target-uri",
        default=DEFAULT_TARGET_URI,
        help="Target MongoDB URI. Defaults to TARGET_MONGO_URI, MONGODB_URI, or MONGO_URI.",
    )
    parser.add_argument(
        "--target-db",
        default=DEFAULT_TARGET_DB,
        help=f"Target MongoDB database name (default: {DEFAULT_TARGET_DB})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Bulk write batch size (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--collections",
        nargs="*",
        default=None,
        help="Optional list of collection names to transfer. By default, all collections are transferred.",
    )
    parser.add_argument(
        "--drop-target",
        action="store_true",
        help="Drop each target collection before writing data and recreating indexes.",
    )
    parser.add_argument(
        "--skip-indexes",
        action="store_true",
        help="Skip copying non-default indexes to the target collections.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if not str(args.target_uri).strip():
        raise ValueError(
            "Target MongoDB URI is required. Set --target-uri or define TARGET_MONGO_URI / MONGODB_URI."
        )
    if int(args.batch_size) <= 0:
        raise ValueError("--batch-size must be greater than 0.")


def ensure_pymongo_available() -> None:
    if PYMONGO_IMPORT_ERROR is not None:
        raise RuntimeError(
            "pymongo is not installed in the active Python environment. "
            "Install dependencies from BrightScraper/requirements.txt before running this script."
        ) from PYMONGO_IMPORT_ERROR


def build_client(uri: str) -> MongoClient[Any]:
    client: MongoClient[Any] = MongoClient(uri, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    return client


def iter_transferable_collection_names(
    source_db: Database[Any],
    requested_names: Iterable[str] | None = None,
) -> list[str]:
    if requested_names:
        requested = {name.strip() for name in requested_names if str(name).strip()}
        return sorted(requested)

    names: list[str] = []
    for metadata in source_db.list_collections():
        name = str(metadata.get("name", "")).strip()
        collection_type = str(metadata.get("type", "collection")).strip().lower()
        if not name or name.startswith("system.") or collection_type != "collection":
            continue
        names.append(name)
    return sorted(names)


def recreate_indexes(source_collection: Collection[Any], target_collection: Collection[Any]) -> None:
    index_models = []
    for index in source_collection.list_indexes():
        document = dict(index)
        if document.get("name") == "_id_":
            continue

        keys = document.pop("key")
        document.pop("ns", None)
        index_models.append((keys, document))

    for keys, options in index_models:
        target_collection.create_index(list(keys.items()), **options)


def flush_batch(target_collection: Collection[Any], operations: list[ReplaceOne[Any]]) -> int:
    if not operations:
        return 0

    target_collection.bulk_write(operations, ordered=False)
    return len(operations)


def transfer_collection(
    source_collection: Collection[Any],
    target_collection: Collection[Any],
    batch_size: int,
) -> CollectionTransferResult:
    result = CollectionTransferResult(name=source_collection.name)
    operations: list[ReplaceOne[Any]] = []

    try:
        for document in source_collection.find({}, no_cursor_timeout=True):
            result.documents_seen += 1
            operations.append(ReplaceOne({"_id": document["_id"]}, document, upsert=True))

            if len(operations) >= batch_size:
                result.documents_written += flush_batch(target_collection, operations)
                operations.clear()

        if operations:
            result.documents_written += flush_batch(target_collection, operations)
    except BulkWriteError as exc:
        result.failed = True
        result.error = str(exc.details)
    except PyMongoError as exc:
        result.failed = True
        result.error = str(exc)

    return result


def main() -> int:
    args = parse_args()

    try:
        validate_args(args)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    try:
        ensure_pymongo_available()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    try:
        source_client = build_client(str(args.source_uri).strip())
        target_client = build_client(str(args.target_uri).strip())
    except PyMongoError as exc:
        print(f"[ERROR] MongoDB connection failed: {exc}", file=sys.stderr)
        return 1

    source_db = source_client[str(args.source_db).strip()]
    target_db = target_client[str(args.target_db).strip()]

    try:
        collection_names = iter_transferable_collection_names(source_db, args.collections)
    except PyMongoError as exc:
        print(f"[ERROR] Failed to list source collections: {exc}", file=sys.stderr)
        source_client.close()
        target_client.close()
        return 1

    if not collection_names:
        print("[INFO] No transferable collections found. Nothing to do.")
        source_client.close()
        target_client.close()
        return 0

    print(f"[INFO] Source DB: {source_db.name}")
    print(f"[INFO] Target DB: {target_db.name}")
    print(f"[INFO] Collections to transfer: {', '.join(collection_names)}")

    failed_collections = 0
    total_seen = 0
    total_written = 0

    try:
        for name in collection_names:
            source_collection = source_db[name]
            target_collection = target_db[name]

            print(f"[INFO] Transferring collection: {name}")

            if args.drop_target:
                target_collection.drop()
                target_collection = target_db[name]

            transfer_result = transfer_collection(
                source_collection=source_collection,
                target_collection=target_collection,
                batch_size=int(args.batch_size),
            )

            total_seen += transfer_result.documents_seen
            total_written += transfer_result.documents_written

            if transfer_result.failed:
                failed_collections += 1
                print(
                    f"[ERROR] Collection '{name}' failed after "
                    f"{transfer_result.documents_seen} documents: {transfer_result.error}",
                    file=sys.stderr,
                )
                continue

            if not args.skip_indexes:
                try:
                    recreate_indexes(source_collection, target_collection)
                except PyMongoError as exc:
                    failed_collections += 1
                    print(
                        f"[ERROR] Collection '{name}' data copied, but index recreation failed: {exc}",
                        file=sys.stderr,
                    )
                    continue

            print(
                f"[INFO] Collection '{name}' complete. "
                f"Read {transfer_result.documents_seen}, wrote {transfer_result.documents_written}."
            )
    except PyMongoError as exc:
        print(f"[ERROR] Transfer aborted: {exc}", file=sys.stderr)
        source_client.close()
        target_client.close()
        return 1
    finally:
        source_client.close()
        target_client.close()

    print(f"[INFO] Total documents read: {total_seen}")
    print(f"[INFO] Total documents written: {total_written}")
    print(f"[INFO] Failed collections: {failed_collections}")

    return 0 if failed_collections == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
