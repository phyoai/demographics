#!/usr/bin/env python3
"""
Reset PostgreSQL tables for Instagram data.
Drops and recreates both tables with proper schema.
"""

import os
import psycopg2
from psycopg2 import sql

# ------------------------------
# CONFIGURATION
# ------------------------------
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_DB = os.getenv("PG_DB", "mydb")
PG_USER = os.getenv("PG_USER", "myuser")
PG_PASSWORD = os.getenv("PG_PASSWORD", "mypassword")

PG_PROFILE_TABLE = os.getenv("PG_PROFILE_TABLE", "instagram_profiles")
PG_POSTS_TABLE = os.getenv("PG_POSTS_TABLE", "instagram_posts_vectors")

# ------------------------------
# Connect to PostgreSQL
# ------------------------------
def connect_db():
    conn = psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        dbname=PG_DB,
        user=PG_USER,
        password=PG_PASSWORD,
    )
    conn.autocommit = True
    return conn

# ------------------------------
# Drop tables (if they exist)
# ------------------------------
def drop_tables(cur):
    # Drop posts table first (due to foreign key dependency)
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(PG_POSTS_TABLE)))
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(PG_PROFILE_TABLE)))
    print(f"Dropped tables '{PG_PROFILE_TABLE}' and '{PG_POSTS_TABLE}' (if they existed).")

# ------------------------------
# Create tables
# ------------------------------
def create_tables(cur):
    # Profile table
    cur.execute(sql.SQL("""
        CREATE TABLE {} (
            username TEXT PRIMARY KEY,
            profile_url TEXT,
            profile_pic_url TEXT,
            full_name TEXT,
            bio TEXT,
            external_url TEXT,
            external_links TEXT[],
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
        )
    """).format(sql.Identifier(PG_PROFILE_TABLE)))
    
    # Posts table with vector column
    cur.execute(sql.SQL("""
        CREATE TABLE {} (
            id SERIAL PRIMARY KEY,
            post_url TEXT UNIQUE,
            shortcode TEXT,
            username TEXT REFERENCES {}(username) ON DELETE CASCADE,
            caption TEXT,
            posted_at TIMESTAMP,
            likes_count INTEGER,
            comments_total INTEGER,
            embedding vector(384)
        )
    """).format(sql.Identifier(PG_POSTS_TABLE), sql.Identifier(PG_PROFILE_TABLE)))
    
    print(f"Created tables '{PG_PROFILE_TABLE}' and '{PG_POSTS_TABLE}' with correct schema.")

# ------------------------------
# Main
# ------------------------------
def main():
    # Optional confirmation
    print("WARNING: This will delete all data in the tables!")
    confirm = input("Type 'yes' to proceed: ")
    if confirm.lower() != 'yes':
        print("Aborted.")
        return
    
    conn = connect_db()
    cur = conn.cursor()
    try:
        drop_tables(cur)
        create_tables(cur)
        print("Reset complete. Tables are ready for fresh data ingestion.")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    main()