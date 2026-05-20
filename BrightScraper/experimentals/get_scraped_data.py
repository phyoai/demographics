import os
import sys
from pathlib import Path

from pymongo import MongoClient
from dotenv import load_dotenv

# Allow running this file directly from BrightScraper/experimentals.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from BrightScraper.services.instagram_profile_llm_analyzer import (
    InstagramProfileLLMAnalyzer,
)
from BrightScraper.instagram.store_in_pgvector import CreatorVectorStore
from BrightScraper.instagram.store_in_pgvector import DATABASE_URL as PGVECTOR_DATABASE_URL




# Load environment variables (e.g., from .env file)
load_dotenv()

# MongoDB connection settings
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DATABASE_NAME = "instagpy"
COLLECTION_NAME = "instagram_scrapes"
LIMIT = 1000


def _coerce_metadata_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    if isinstance(value, str) and value.strip():
        return [value.strip()]

    return []


def _stringify_metadata_list(values):
    if not isinstance(values, list):
        return None

    cleaned_values = [str(item).strip() for item in values if str(item).strip()]
    if not cleaned_values:
        return None

    return ", ".join(cleaned_values)


def build_creator_payload(doc, profile_analysis):
    result = doc.get("result", {})
    if not isinstance(result, dict):
        result = {}

    profile = result.get("profile", {})
    if not isinstance(profile, dict):
        profile = {}

    username = profile.get("username") or doc.get("requested_username")
    if not username:
        raise ValueError("Profile username is missing")

    niches = _coerce_metadata_list(profile_analysis.get("niche"))
    categories = _coerce_metadata_list(
        profile_analysis.get("category") or profile.get("category")
    )

    return {
        "id": doc.get("_id"),
        "username": username,
        "profile_url": profile.get("profile_url"),
        "profile_picture": profile.get("profile_pic_url"),
        "full_name": profile.get("full_name"),
        "bio": profile.get("bio") or profile.get("biography"),
        "followers": profile.get("followers_count"),
        "following": profile.get("following_count"),
        "posts": profile.get("posts_count"),
        "is_verified": profile.get("is_verified"),
        "external_links": profile.get("external_links") or [],
        "country": profile_analysis.get("country"),
        "city": profile_analysis.get("city"),
        "niche": niches,
        "category": categories,
        "profile_summary": profile_analysis.get("profile_summary"),
        "analysis_source": profile_analysis.get("analysis_source"),
        "analysis_model": profile_analysis.get("analysis_model"),
        "raw_profile": profile,
        "raw_result": result,
    }


def fetch_records_without_load():
    """Fetch up to 1000 records from MongoDB using a cursor (low memory footprint)."""
    client = None
    try:
        # Connect to MongoDB
        client = MongoClient(MONGO_URI)
        db = client[DATABASE_NAME]
        collection = db[COLLECTION_NAME]

        # Optional: ensure an index on the field you sort/query (improves performance)
        # collection.create_index("timestamp")  # or any field you frequently filter by

        # Create a cursor with limit and batch_size
        # - limit(1000) restricts total documents returned
        # - batch_size(100) tells MongoDB to send 100 docs per network round trip
        # - no_cursoe_timeout prevents cursor expiry if processing is slow
        cursor = collection.find().limit(LIMIT).batch_size(100)
        # Process documents one by one – minimal memory load
        
        profile_analyzer = InstagramProfileLLMAnalyzer()
        if not profile_analyzer.is_enabled:
            print("OpenAI profile analyzer is disabled. Set OPENAI_API_KEY to enable LLM predictions.")

        vector_store = CreatorVectorStore(PGVECTOR_DATABASE_URL)
        vector_store.setup_database()

        count = 0
        stored_count = 0
        failed_count = 0
        stored_results = []
        debugging=[]
        for doc in cursor:
            # Do something with each document (e.g., print, analyse, save to file)
            # For demonstration, we just print the _id
            print(f"Processing record {count+1}: {doc.get('_id')}")

            try:
                profile_analysis = profile_analyzer.analyze(doc)
                creator_payload = build_creator_payload(doc, profile_analysis)
                debugging.append(creator_payload)
                vector_store_payload = {
                    **creator_payload,
                    "niche": _stringify_metadata_list(creator_payload.get("niche")),
                    "category": _stringify_metadata_list(creator_payload.get("category")),
                }
                store_result = vector_store.upsert_creator(vector_store_payload)
                stored_results.append(
                    {
                        **store_result,
                        "analysis_source": creator_payload.get("analysis_source"),
                        "analysis_model": creator_payload.get("analysis_model"),
                    }
                )
                stored_count += 1
                print(
                    "Stored profile:",
                    creator_payload["username"],
                    f"(followers={creator_payload.get('followers') or 0})",
                )
            except Exception as exc:
                failed_count += 1
                print(f"Failed to store record {doc.get('_id')}: {exc}")

            count += 1

        print(
            f"\nProcessed {count} records. "
            f"Stored={stored_count}, Failed={failed_count}."
        )
        return stored_results

    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        # Always close the cursor and connection
        if client:
            client.close()

if __name__ == "__main__":
    fetch_records_without_load()
