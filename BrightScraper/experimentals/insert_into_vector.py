#!/usr/bin/env python3
"""
Ingest Instagram scrape data from MongoDB into PostgreSQL with pgvector.
Extracts profile info and posts with embeddings.
"""

import os
from pymongo import MongoClient
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
from pgvector.psycopg2 import register_vector
from sentence_transformers import SentenceTransformer

# ------------------------------
# CONFIGURATION (override via env vars)
# ------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "instagpy")
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "instagram_scrapes")

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_DB = os.getenv("PG_DB", "mydb")
PG_USER = os.getenv("PG_USER", "myuser")
PG_PASSWORD = os.getenv("PG_PASSWORD", "mypassword")
PG_PROFILE_TABLE = os.getenv("PG_PROFILE_TABLE", "instagram_profiles")
PG_POSTS_TABLE = os.getenv("PG_POSTS_TABLE", "instagram_posts_vectors")

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))
MODEL_NAME = os.getenv("MODEL_NAME", "all-MiniLM-L6-v2")   # 384-dim embeddings

# ------------------------------
# PostgreSQL helpers
# ------------------------------
def ensure_pgvector_extension(conn) -> None:
    """Enable pgvector extension in the database."""
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


def connect_postgres():
    conn = psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        dbname=PG_DB,
        user=PG_USER,
        password=PG_PASSWORD,
    )
    ensure_pgvector_extension(conn)
    register_vector(conn)      # enables vector type in Python
    conn.commit()
    return conn


def create_profile_table(cur, table_name: str) -> None:
    """Create table for Instagram profile data."""
    cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {table_name} (
            username TEXT PRIMARY KEY,
            profile_url TEXT,
            profile_pic_url TEXT,
            full_name TEXT,
            bio TEXT,
            external_url TEXT,
            external_links TEXT[],      -- array of URLs
            followers_count INTEGER,
            following_count INTEGER,
            posts_count INTEGER,
            is_verified BOOLEAN,
            is_private BOOLEAN,
            category TEXT,
            business_or_creator_label TEXT,
            highlights_count INTEGER,
            scraped_at TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """).format(table_name=sql.Identifier(table_name)))


def create_posts_table(cur, posts_table: str, profile_table: str) -> None:
    """Create table for posts with vector embedding column."""
    cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {posts_table} (
            id SERIAL PRIMARY KEY,
            post_url TEXT UNIQUE,
            shortcode TEXT,
            username TEXT REFERENCES {profile_table}(username),
            caption TEXT,
            posted_at TIMESTAMP,
            likes_count INTEGER,
            comments_total INTEGER,
            embedding vector(384)         -- matches all-MiniLM-L6-v2 output
        );
    """).format(
        posts_table=sql.Identifier(posts_table),
        profile_table=sql.Identifier(profile_table)
    ))


def upsert_profile(cur, profile: dict) -> None:
    """Insert or update profile information (upsert by username)."""
    sql_stmt = f"""
        INSERT INTO {PG_PROFILE_TABLE} (
            username, profile_url, profile_pic_url, full_name, bio,
            external_url, external_links, followers_count, following_count,
            posts_count, is_verified, is_private, category,
            business_or_creator_label, highlights_count, scraped_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (username) DO UPDATE SET
            profile_url = EXCLUDED.profile_url,
            profile_pic_url = EXCLUDED.profile_pic_url,
            full_name = EXCLUDED.full_name,
            bio = EXCLUDED.bio,
            external_url = EXCLUDED.external_url,
            external_links = EXCLUDED.external_links,
            followers_count = EXCLUDED.followers_count,
            following_count = EXCLUDED.following_count,
            posts_count = EXCLUDED.posts_count,
            is_verified = EXCLUDED.is_verified,
            is_private = EXCLUDED.is_private,
            category = EXCLUDED.category,
            business_or_creator_label = EXCLUDED.business_or_creator_label,
            highlights_count = EXCLUDED.highlights_count,
            scraped_at = EXCLUDED.scraped_at,
            updated_at = CURRENT_TIMESTAMP
    """
    cur.execute(sql_stmt, (
        profile.get("username"),
        profile.get("profile_url"),
        profile.get("profile_pic_url"),
        profile.get("full_name"),
        profile.get("bio"),
        profile.get("external_url"),
        profile.get("external_links", []),
        profile.get("followers_count"),
        profile.get("following_count"),
        profile.get("posts_count"),
        profile.get("is_verified"),
        profile.get("is_private"),
        profile.get("category"),
        profile.get("business_or_creator_label"),
        profile.get("highlights_count"),
        profile.get("scraped_at")
    ))


def insert_post_batch(cur, table_name: str, batch: list) -> None:
    """Insert a batch of posts (with embeddings)."""
    insert_sql = sql.SQL("""
        INSERT INTO {table_name}
        (post_url, shortcode, username, caption, posted_at, likes_count, comments_total, embedding)
        VALUES %s
        ON CONFLICT (post_url) DO NOTHING
    """).format(table_name=sql.Identifier(table_name))
    execute_values(
        cur,
        insert_sql,
        batch,
        template="(%s, %s, %s, %s, %s, %s, %s, %s::vector)",
    )


def generate_embedding(model: SentenceTransformer, text: str):
    """Generate embedding vector from text using the loaded model."""
    return model.encode(text or "").tolist()


# ------------------------------
# Main ingestion pipeline
# ------------------------------
def main():
    # Connect to MongoDB
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client[MONGO_DB]
    collection = db[MONGO_COLLECTION]

    # Connect to PostgreSQL
    pg_conn = connect_postgres()
    pg_cur = pg_conn.cursor()

    try:
        # Create tables if they don't exist
        create_profile_table(pg_cur, PG_PROFILE_TABLE)
        create_posts_table(pg_cur, PG_POSTS_TABLE, PG_PROFILE_TABLE)
        pg_conn.commit()

        # Load embedding model
        print(f"Loading embedding model: {MODEL_NAME}")
        model = SentenceTransformer(MODEL_NAME)
        print("Model ready.")

        # Process each MongoDB document
        docs = collection.find({})
        total_posts = 0
        post_batch = []

        for doc in docs:
            result = doc.get("result", {})
            profile_data = result.get("profile")
            if not profile_data:
                print("Skipping document without profile data")
                continue

            # Extract profile fields (what you requested)
            profile = {
                "username": profile_data.get("username"),
                "profile_url": profile_data.get("profile_url"),
                "profile_pic_url": profile_data.get("profile_pic_url"),
                "full_name": profile_data.get("full_name"),
                "bio": profile_data.get("bio"),
                "external_url": profile_data.get("external_url"),
                "external_links": profile_data.get("external_links", []),
                "followers_count": profile_data.get("followers_count"),
                "following_count": profile_data.get("following_count"),
                "posts_count": profile_data.get("posts_count"),
                "is_verified": profile_data.get("is_verified"),
                "is_private": profile_data.get("is_private"),
                "category": profile_data.get("category"),
                "business_or_creator_label": profile_data.get("business_or_creator_label"),
                "highlights_count": profile_data.get("highlights_count"),
                "scraped_at": result.get("scraped_at") or doc.get("scraped_at")
            }
            # Insert/update profile
            upsert_profile(pg_cur, profile)
            pg_conn.commit()   # commit profile immediately

            # Process posts
            posts = result.get("posts", [])
            for post in posts:
                # Use caption; fallback to alt text if missing
                caption = post.get("caption") or post.get("alt", "")
                embedding = generate_embedding(model, caption)

                row = (
                    post.get("post_url"),
                    post.get("shortcode"),
                    profile["username"],
                    caption,
                    post.get("posted_at"),
                    post.get("likes_count"),
                    post.get("comments_count_total"),
                    embedding,
                )
                post_batch.append(row)
                total_posts += 1

                if len(post_batch) >= BATCH_SIZE:
                    insert_post_batch(pg_cur, PG_POSTS_TABLE, post_batch)
                    pg_conn.commit()
                    print(f"Inserted batch. Total posts so far: {total_posts}")
                    post_batch = []

        # Insert any remaining posts
        if post_batch:
            insert_post_batch(pg_cur, PG_POSTS_TABLE, post_batch)
            pg_conn.commit()
            print(f"Inserted final batch. Total posts: {total_posts}")

    finally:
        pg_cur.close()
        pg_conn.close()
        mongo_client.close()

    print("Ingestion complete!")


if __name__ == "__main__":
    main()
    
    
    
    # docker run --name pgvector-container `
    #     -e POSTGRES_USER=myuser `
    #     -e POSTGRES_PASSWORD=mypassword `
    #     -e POSTGRES_DB=mydb `
    #     -p 5432:5432 `
    #     -d pgvector/pgvector:pg16