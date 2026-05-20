#!/usr/bin/env python3
"""
Vector search module for Instagram posts stored in PostgreSQL with pgvector.
Supports filtering by profile attributes (followers count, bio keywords).
"""

import os
from functools import lru_cache

import psycopg2
from psycopg2 import sql
from pgvector.psycopg2 import register_vector
from sentence_transformers import SentenceTransformer

# ------------------------------
# CONFIGURATION
# ------------------------------
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_DB = os.getenv("PG_DB", "mydb")
PG_USER = os.getenv("PG_USER", "myuser")
PG_PASSWORD = os.getenv("PG_PASSWORD", "mypassword")
PG_POSTS_TABLE = os.getenv("PG_POSTS_TABLE", "instagram_posts_vectors")
PG_PROFILE_TABLE = os.getenv("PG_PROFILE_TABLE", "instagram_profiles")

MODEL_NAME = os.getenv("MODEL_NAME", "all-MiniLM-L6-v2")
TOP_K = int(os.getenv("TOP_K", "5"))

# ------------------------------
# CONNECTION & MODEL
# ------------------------------
def ensure_pgvector_extension(conn) -> None:
    """Enable pgvector extension before registering the vector adapter."""
    autocommit_state = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    except psycopg2.Error as exc:
        raise RuntimeError(
            "pgvector extension not available. Install it in your PostgreSQL instance."
        ) from exc
    finally:
        conn.autocommit = autocommit_state


def get_db_connection():
    conn = psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        dbname=PG_DB,
        user=PG_USER,
        password=PG_PASSWORD
    )
    ensure_pgvector_extension(conn)
    register_vector(conn)
    conn.commit()
    return conn


@lru_cache(maxsize=1)
def load_model():
    print(f"Loading embedding model: {MODEL_NAME}")
    return SentenceTransformer(MODEL_NAME)


def encode_query(model: SentenceTransformer, query_text: str) -> list[float]:
    embedding = model.encode(query_text)
    if hasattr(embedding, "tolist"):
        return embedding.tolist()
    return list(embedding)

# ------------------------------
# SEARCH FUNCTION (with optional profile filters)
# ------------------------------
def search(
    query_text,
    top_k=TOP_K,
    min_followers=None,
    text_keyword=None,
    bio_keyword=None,
):
    """
    Perform semantic search over Instagram posts with optional profile filters.
    
    Parameters:
        query_text (str): Search query.
        top_k (int): Number of results.
        min_followers (int, optional): Minimum followers count of the author.
        text_keyword (str, optional): Case-insensitive keyword to match in
            profile fields or post captions.
        bio_keyword (str, optional): Backward-compatible alias for text_keyword.
    
    Returns:
        list of dict: Results with similarity, post details, and author info.
    """
    if text_keyword is None and bio_keyword:
        text_keyword = bio_keyword

    model = load_model()
    query_embedding = encode_query(model, query_text)
    
    conn = get_db_connection()
    cur = conn.cursor()

    try:
        # Base SQL: join posts with profiles
        query = sql.SQL("""
            SELECT 
                p.post_url, p.shortcode, p.username, p.caption, p.posted_at, p.likes_count,
                pr.followers_count, pr.bio,
                1 - (p.embedding <=> %s::vector) AS similarity
            FROM {posts_table} p
            JOIN {profile_table} pr ON p.username = pr.username
            WHERE 1=1
        """).format(
            posts_table=sql.Identifier(PG_POSTS_TABLE),
            profile_table=sql.Identifier(PG_PROFILE_TABLE),
        )
        params = [query_embedding]

        # Apply filters
        if min_followers is not None:
            query += sql.SQL(" AND pr.followers_count >= %s")
            params.append(min_followers)
        if text_keyword:
            query += sql.SQL("""
                AND (
                    COALESCE(LOWER(pr.username), '') LIKE %s OR
                    COALESCE(LOWER(pr.full_name), '') LIKE %s OR
                    COALESCE(LOWER(pr.bio), '') LIKE %s OR
                    COALESCE(LOWER(pr.category), '') LIKE %s OR
                    COALESCE(LOWER(pr.business_or_creator_label), '') LIKE %s OR
                    COALESCE(LOWER(p.caption), '') LIKE %s
                )
            """)
            keyword_pattern = f"%{text_keyword.lower()}%"
            params.extend([keyword_pattern] * 6)

        query += sql.SQL(" ORDER BY p.embedding <=> %s::vector LIMIT %s")
        params.extend([query_embedding, top_k])

        cur.execute(query, params)
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    
    results = []
    for row in rows:
        results.append({
            "similarity": row[8],          # computed similarity
            "post_url": row[0],
            "shortcode": row[1],
            "username": row[2],
            "caption": row[3],
            "posted_at": row[4],
            "likes_count": row[5],
            "followers_count": row[6],
            "bio": row[7]
        })
    return results

# ------------------------------
# EXAMPLE USAGE
# ------------------------------
if __name__ == "__main__":
    query = "influencers in delhi with 100k followers"
    print(f"Searching for: {query}\n")
    
    # Example: filter for profiles with ≥100k followers and "delhi" in bio
    results = search(
        query_text=query,
        top_k=5,
        min_followers=100000,
        text_keyword="delhi"
    )
    
    if not results:
        print("No results found. Try relaxing the filters.")
    else:
        for i, res in enumerate(results, 1):
            print(f"{i}. Similarity: {res['similarity']:.4f}")
            followers_count = res["followers_count"] or 0
            bio = res["bio"] or ""
            caption = res["caption"] or ""
            print(f"   Username: {res['username']} (Followers: {followers_count:,})")
            print(f"   Bio: {bio[:100]}...")
            print(f"   Post URL: {res['post_url']}")
            print(f"   Posted: {res['posted_at']}")
            print(f"   Likes: {(res['likes_count'] or 0):,}")
            caption_preview = (caption[:200] + "...") if len(caption) > 200 else caption
            print(f"   Caption: {caption_preview}\n")
