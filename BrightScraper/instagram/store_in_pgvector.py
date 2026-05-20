import os
import json
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List
import re
import psycopg
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv


load_dotenv()


TABLE_NAME = "instagram_creators"
EMBEDDING_MODEL_NAME = os.getenv(
    "CREATOR_EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-MiniLM-L3-v2",
)
EMBEDDING_DIM = int(os.getenv("CREATOR_EMBEDDING_DIM", "384"))
DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/creator_db"


class CreatorVectorStore:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        self.embedding_dim = self.model.get_embedding_dimension()

        if self.embedding_dim != EMBEDDING_DIM:
            raise ValueError(
                f"Configured embedding dim {EMBEDDING_DIM} does not match "
                f"model dimension {self.embedding_dim} for {EMBEDDING_MODEL_NAME}"
            )

    def connect(self):
        conn = psycopg.connect(self.database_url, autocommit=True)
        return conn

    def setup_database(self):
        """
        1. Enable pgvector extension.
        2. Check if table exists.
        3. Create table if missing.
        4. Create indexes.
        """

        with self.connect() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            register_vector(conn)

            table_exists = self.table_exists(conn)

            if not table_exists:
                self.create_table(conn)

            self.create_indexes(conn)

    def table_exists(self, conn) -> bool:
        query = """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_name = %s
        );
        """

        result = conn.execute(query, (TABLE_NAME,)).fetchone()
        return bool(result[0])

    def create_table(self, conn):
        query = f"""
        CREATE TABLE {TABLE_NAME} (
            id TEXT PRIMARY KEY,

            username TEXT UNIQUE NOT NULL,
            profile_url TEXT,
            profile_picture TEXT,
            full_name TEXT,
            bio TEXT,

            followers INTEGER DEFAULT 0,
            following INTEGER DEFAULT 0,
            posts_count INTEGER DEFAULT 0,
            is_verified BOOLEAN DEFAULT FALSE,

            external_links JSONB DEFAULT '[]'::jsonb,

            country TEXT,
            city TEXT,

            niche TEXT,
            category TEXT,
            profile_summary TEXT,

            search_text TEXT NOT NULL,
            embedding VECTOR({EMBEDDING_DIM}) NOT NULL,

            raw_data JSONB NOT NULL,

            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        """

        conn.execute(query)

    def create_indexes(self, conn):
        indexes = [
            f"""
            CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_followers
            ON {TABLE_NAME} (followers);
            """,
            f"""
            CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_category
            ON {TABLE_NAME} (category);
            """,
            f"""
            CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_country
            ON {TABLE_NAME} (country);
            """,
            f"""
            CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_city
            ON {TABLE_NAME} (city);
            """,
            f"""
            CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_niche
            ON {TABLE_NAME} (niche);
            """,
            f"""
            CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_raw_data_gin
            ON {TABLE_NAME} USING GIN (raw_data);
            """,
            f"""
            CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_embedding_hnsw
            ON {TABLE_NAME}
            USING hnsw (embedding vector_cosine_ops);
            """
        ]

        for index_query in indexes:
            conn.execute(index_query)

    def normalize_creator(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Converts Mongo/ObjectId-style creator dict into clean DB-ready dict.
        """

        creator_id = self.safe_str(data.get("id") or data.get("_id"))

        if not creator_id:
            username_for_id = data.get("username") or data.get("profile_url") or json.dumps(data, sort_keys=True)
            creator_id = hashlib.sha256(username_for_id.encode("utf-8")).hexdigest()

        username = self.safe_str(data.get("username"))

        if not username:
            raise ValueError("username is required")

        normalized = {
            "id": creator_id,
            "username": username,
            "profile_url": self.safe_str(data.get("profile_url")),
            "profile_picture": self.safe_str(data.get("profile_picture")),
            "full_name": self.safe_str(data.get("full_name")),
            "bio": self.safe_str(data.get("bio")),
            "followers": self.safe_int(data.get("followers")),
            "following": self.safe_int(data.get("following")),
            "posts_count": self.safe_int(data.get("posts")),
            "is_verified": bool(data.get("is_verified", False)),
            "external_links": data.get("external_links") or [],
            "country": self.safe_str(data.get("country")),
            "city": self.safe_str(data.get("city")),
            "niche": self.safe_str(data.get("niche")),
            "category": self.safe_str(data.get("category")),
            "profile_summary": self.safe_str(data.get("profile_summary")),
            "raw_data": self.make_json_safe(data),
        }

        normalized["search_text"] = self.build_search_text(normalized)
        normalized["embedding"] = self.embed_text(normalized["search_text"])

        return normalized

    def build_search_text(self, creator: Dict[str, Any]) -> str:
        """
        Build the text used for semantic embedding.

        The embedding now focuses primarily on profile summary plus creator size
        signals from followers/following, which tends to rank creator-intent
        queries better than embedding the entire profile blob.
        """
        followers = self.safe_int(creator.get("followers"))
        following = self.safe_int(creator.get("following"))

        summary = self.safe_str(creator.get("profile_summary"))
        if not summary:
            fallback_parts = [
                self.safe_str(creator.get("category")),
                self.safe_str(creator.get("niche")),
                self.safe_str(creator.get("bio")),
                self.safe_str(creator.get("country")),
                self.safe_str(creator.get("city")),
            ]
            summary = ". ".join(part for part in fallback_parts if part)

        parts = [
            f"Profile summary: {summary or 'Unknown creator profile'}",
            f"Followers count: {followers}",
            f"Followers approx: {self.humanize_count(followers)}",
            f"Audience size: {self.describe_follower_band(followers)}",
            f"Following count: {following}",
            f"Following approx: {self.humanize_count(following)}",
            f"Following behavior: {self.describe_following_band(following)}",
        ]

        category = self.safe_str(creator.get("category"))
        if category:
            parts.append(f"Category context: {category}")

        niche = self.safe_str(creator.get("niche"))
        if niche:
            parts.append(f"Niche context: {niche}")

        country = self.safe_str(creator.get("country"))
        city = self.safe_str(creator.get("city"))
        if country:
            parts.append(f"Country: {country}")
        if city:
            parts.append(f"City: {city}")

        return "\n".join(part for part in parts if part)

    def humanize_count(self, value: int) -> str:
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if value >= 1_000:
            return f"{value / 1_000:.1f}K"
        return str(value)

    def describe_follower_band(self, followers: int) -> str:
        if followers >= 1_000_000:
            return "mega influencer"
        if followers >= 100_000:
            return "macro influencer"
        if followers >= 10_000:
            return "mid-tier creator"
        if followers >= 1_000:
            return "micro creator"
        return "emerging creator"

    def describe_following_band(self, following: int) -> str:
        if following <= 100:
            return "very selective following list"
        if following <= 500:
            return "selective following list"
        if following <= 2000:
            return "moderate following list"
        return "broad following list"

    def embed_text(self, text: str) -> List[float]:
        embedding = self.model.encode(
            text,
            convert_to_tensor=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )

        return embedding.detach().cpu().float().tolist()

    def to_vector_literal(self, embedding: List[float]) -> str:
        if not embedding:
            raise ValueError("embedding cannot be empty")

        return "[" + ",".join(f"{float(value):.8f}" for value in embedding) + "]"

    def upsert_creator(self, data: Dict[str, Any]) -> Dict[str, Any]:
        creator = self.normalize_creator(data)

        query = f"""
        INSERT INTO {TABLE_NAME} (
            id,
            username,
            profile_url,
            profile_picture,
            full_name,
            bio,
            followers,
            following,
            posts_count,
            is_verified,
            external_links,
            country,
            city,
            niche,
            category,
            profile_summary,
            search_text,
            embedding,
            raw_data,
            created_at,
            updated_at
        )
        VALUES (
            %(id)s,
            %(username)s,
            %(profile_url)s,
            %(profile_picture)s,
            %(full_name)s,
            %(bio)s,
            %(followers)s,
            %(following)s,
            %(posts_count)s,
            %(is_verified)s,
            %(external_links)s::jsonb,
            %(country)s,
            %(city)s,
            %(niche)s,
            %(category)s,
            %(profile_summary)s,
            %(search_text)s,
            %(embedding)s,
            %(raw_data)s::jsonb,
            NOW(),
            NOW()
        )
        ON CONFLICT (username)
        DO UPDATE SET
            profile_url = EXCLUDED.profile_url,
            profile_picture = EXCLUDED.profile_picture,
            full_name = EXCLUDED.full_name,
            bio = EXCLUDED.bio,
            followers = EXCLUDED.followers,
            following = EXCLUDED.following,
            posts_count = EXCLUDED.posts_count,
            is_verified = EXCLUDED.is_verified,
            external_links = EXCLUDED.external_links,
            country = EXCLUDED.country,
            city = EXCLUDED.city,
            niche = EXCLUDED.niche,
            category = EXCLUDED.category,
            profile_summary = EXCLUDED.profile_summary,
            search_text = EXCLUDED.search_text,
            embedding = EXCLUDED.embedding,
            raw_data = EXCLUDED.raw_data,
            updated_at = NOW()
        RETURNING id, username, followers, category, niche, city, country;
        """

        payload = {
            **creator,
            "external_links": json.dumps(creator["external_links"], ensure_ascii=False),
            "raw_data": json.dumps(creator["raw_data"], ensure_ascii=False),
        }

        with self.connect() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            register_vector(conn)

            result = conn.execute(query, payload).fetchone()

        return {
            "id": result[0],
            "username": result[1],
            "followers": result[2],
            "category": result[3],
            "niche": result[4],
            "city": result[5],
            "country": result[6],
            "status": "stored_or_updated",
        }

    def safe_str(self, value: Any) -> Optional[str]:
        if value is None:
            return None

        value = str(value).strip()

        if value in {"", "None", "null"}:
            return None

        return value

    def safe_int(self, value: Any) -> int:
        try:
            if value is None:
                return 0
            return int(value)
        except Exception:
            return 0

    def make_json_safe(self, value: Any) -> Any:
        """
        Converts ObjectId/datetime/unknown objects into JSON-safe values.
        """

        if value is None:
            return None

        if isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()

        if isinstance(value, list):
            return [self.make_json_safe(v) for v in value]

        if isinstance(value, tuple):
            return [self.make_json_safe(v) for v in value]

        if isinstance(value, dict):
            return {
                str(k): self.make_json_safe(v)
                for k, v in value.items()
            }

        return str(value)
    
    def parse_creator_query(self, user_query: str) -> Dict[str, Any]:
        """
        Parses natural query like:
        - "I want 3 food creators with 100k followers"
        - "give me 5 comedy creators from India"
        - "find 10 fashion influencers in Mumbai with 50k followers"
        """

        query = user_query.lower()

        # default limit
        limit = 10

        # find requested count: "3 food creators", "give me 5..."
        count_match = re.search(r"\b(?:give me|want|show|get|find)?\s*(\d{1,2})\b", query)
        if count_match:
            limit = int(count_match.group(1))

        # followers filter: 100k, 1m, 50000 followers
        min_followers = None

        followers_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(k|m|thousand|million)?\s*(?:followers|follower)?",
            query,
        )

        if followers_match:
            number = float(followers_match.group(1))
            suffix = followers_match.group(2)

            if suffix in {"k", "thousand"}:
                min_followers = int(number * 1000)
            elif suffix in {"m", "million"}:
                min_followers = int(number * 1_000_000)
            else:
                # only treat as followers if word follower exists nearby
                if "follower" in followers_match.group(0):
                    min_followers = int(number)

        # simple country/city hints
        country = None
        city = None

        # Keep this flexible. Exact country/city filtering should come from UI filters later.
        if "india" in query or "indian" in query:
            country = "India"

        # optional common city extraction from phrase "in Mumbai", "from Delhi"
        city_match = re.search(r"\b(?:in|from|based in)\s+([a-zA-Z ]{3,30})", user_query, re.IGNORECASE)
        if city_match:
            possible_city = city_match.group(1).strip()

            # remove trailing filter words
            possible_city = re.sub(
                r"\b(with|having|above|over|under|followers|creator|creators|influencer|influencers).*",
                "",
                possible_city,
                flags=re.IGNORECASE,
            ).strip()

            if possible_city and possible_city.lower() not in {"india", "indian"}:
                city = possible_city.title()

        return {
            "semantic_query": user_query,
            "limit": limit,
            "min_followers": min_followers,
            "country": country,
            "city": city,
        }


    def retrieve_creators(
        self,
        user_query: str,
        limit: Optional[int] = None,
        min_followers: Optional[int] = None,
        country: Optional[str] = None,
        city: Optional[str] = None,
        min_similarity: float = 0.20,
    ) -> List[Dict[str, Any]]:
        """
        Hybrid retrieval:
        - semantic search using pgvector
        - SQL filters for followers, city, country
        - returns ranked creators
        """

        parsed = self.parse_creator_query(user_query)

        final_limit = limit or parsed["limit"]
        final_min_followers = min_followers if min_followers is not None else parsed["min_followers"]
        final_country = country or parsed["country"]
        final_city = city or parsed["city"]

        query_embedding = self.to_vector_literal(
            self.embed_text(parsed["semantic_query"])
        )

        where_clauses = [
            "(1 - (embedding <=> %(query_embedding)s::vector)) >= %(min_similarity)s"
        ]

        params = {
            "query_embedding": query_embedding,
            "min_similarity": min_similarity,
            "limit": final_limit,
        }

        if final_min_followers is not None:
            where_clauses.append("followers >= %(min_followers)s")
            params["min_followers"] = final_min_followers

        if final_country:
            where_clauses.append("country ILIKE %(country)s")
            params["country"] = final_country

        if final_city:
            where_clauses.append("city ILIKE %(city)s")
            params["city"] = final_city

        where_sql = "\n            AND ".join(where_clauses)

        sql = f"""
        SELECT
            id,
            username,
            profile_url,
            profile_picture,
            full_name,
            bio,
            followers,
            following,
            posts_count,
            is_verified,
            external_links,
            country,
            city,
            niche,
            category,
            profile_summary,
            search_text,

            -- cosine distance: lower is better
            embedding <=> %(query_embedding)s::vector AS distance,

            -- similarity: higher is better
            1 - (embedding <=> %(query_embedding)s::vector) AS similarity

        FROM {TABLE_NAME}
        WHERE
            {where_sql}
        ORDER BY embedding <=> %(query_embedding)s::vector ASC
        LIMIT %(limit)s;
        """

        with self.connect() as conn:
            register_vector(conn)
            rows = conn.execute(sql, params).fetchall()

        results = []

        for row in rows:
            results.append({
                "id": row[0],
                "username": row[1],
                "profile_url": row[2],
                "profile_picture": row[3],
                "full_name": row[4],
                "bio": row[5],
                "followers": row[6],
                "following": row[7],
                "posts_count": row[8],
                "is_verified": row[9],
                "external_links": row[10],
                "country": row[11],
                "city": row[12],
                "niche": row[13],
                "category": row[14],
                "profile_summary": row[15],
                "search_text": row[16],
                "distance": float(row[17]),
                "similarity": round(float(row[18]), 4),
            })

        return results


if __name__ == "__main__":

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing in .env")

    creator_data = {
        "id": "6a01a56361b2868976e5f9fc",
        "username": "anshul.jeet444",
        "profile_url": "https://www.instagram.com/anshul.jeet444/",
        "profile_picture": "https://scontent.cdninstagram.com/v/t51.82787-19/528747307_18086258662836292_7659532512174939117_n.jpg",
        "full_name": "Anshul Jeet",
        "bio": "38\nArtist\n@netflix_in\n@thepyromedia\nMilestone'❤️‍🔥\nBloopers!\nEvent!👻\nZzZ\nFan arts🎀\nHighlights\nEdits!⚡️\nCeleb🫶🏻\nReplies!💙",
        "followers": 375000,
        "following": 51,
        "posts": 198,
        "is_verified": False,
        "external_links": [
            "https://yt.openinapp.co/anshuljeet"
        ],
        "country": None,
        "city": None,
        "niche": "Comedy reels / entertainment creator",
        "category": "Creator",
        "profile_summary": "Hindi-language comedy and entertainment reels creator focused on viral skits, edits, bloopers, and fan-art style content. Likely targets a young Indian social audience and brand collaborations around media, events, and consumer promotions."
    }

    store = CreatorVectorStore(DATABASE_URL)

    store.setup_database()

    # result = store.upsert_creator(creator_data)

    # print(json.dumps(result, indent=2, ensure_ascii=False))


    results = store.retrieve_creators(
        "I want three food creators with 100k followers"
    )

    print(json.dumps(results, indent=2, ensure_ascii=False))
