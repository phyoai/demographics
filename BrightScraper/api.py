from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
import httpx


try:
    from .api_models import (
        AnalyticsRequest,
        CreatorSearchRequest,
        DemographicsAnalyzeRequest,
        InstagramProfileDataRequest,
        LocationLookupRequest,
        ProfileScrapeRequest,
        ReverseLocationLookupRequest,
        SearchPayload,
    )
    from .instagram.apify_post_details import (
        INSTAGRAM_PROFILES_DATA_COLLECTION,
        fetch_and_store_profile_data_blocking,
        get_instagram_profiles_data_collection,
        load_instagram_profile_data_from_db,
        normalize_username as normalize_instagram_username,
    )
    from .instagram.creator_search_openai import run_agent as run_creator_search_agent
    from .services.analytics_helpers import build_user_analytics
    from .services.demographics_analysis_service import (
        ANALYZE_CACHE_DB_COLLECTION,
        ANALYZE_CACHE_DIR,
        ANALYZE_CACHE_TTL_SECONDS,
        STORED_SCRAPES_COLLECTION,
        STORED_SCRAPES_DB_NAME,
        build_error_envelope,
        cleanup_expired_analyze_jobs,
        create_analyze_job,
        execute_analysis_pipeline,
        execute_stored_analysis_pipeline,
        get_analyze_db_collection,
        get_analyze_job,
        get_stored_scrapes_collection,
        load_analyze_cache,
        load_analyze_cache_from_db,
        run_analyze_job,
        save_analyze_cache,
        save_analyze_cache_to_db,
        serialize_analyze_job_payload,
        utc_now_iso,
    )
    from .services.api_support import (
        configure_logfire_for_app,
        normalize_deadline_seconds,
        normalize_input_usernames,
        normalize_location_queries,
        normalize_reverse_location_points,
        verify_api_key,
    )
    from .services.get_location import (
        resolve_location_requests as resolve_location_requests_service,
        resolve_reverse_location_requests as resolve_reverse_location_requests_service,
    )
    from .services import google_search_headless as google_search_headless_service
    from .services.instagram_profile_data_service import (
        resolve_instagram_profile_data_usernames as resolve_instagram_profile_data_usernames_service,
    )
except ImportError:  # pragma: no cover
    from api_models import (
        AnalyticsRequest,
        CreatorSearchRequest,
        DemographicsAnalyzeRequest,
        InstagramProfileDataRequest,
        LocationLookupRequest,
        ProfileScrapeRequest,
        ReverseLocationLookupRequest,
        SearchPayload,
    )
    from instagram.apify_post_details import (
        INSTAGRAM_PROFILES_DATA_COLLECTION,
        fetch_and_store_profile_data_blocking,
        get_instagram_profiles_data_collection,
        load_instagram_profile_data_from_db,
        normalize_username as normalize_instagram_username,
    )
    from instagram.creator_search_openai import run_agent as run_creator_search_agent
    from services.analytics_helpers import build_user_analytics
    from services.demographics_analysis_service import (
        ANALYZE_CACHE_DB_COLLECTION,
        ANALYZE_CACHE_DIR,
        ANALYZE_CACHE_TTL_SECONDS,
        STORED_SCRAPES_COLLECTION,
        STORED_SCRAPES_DB_NAME,
        build_error_envelope,
        cleanup_expired_analyze_jobs,
        create_analyze_job,
        execute_analysis_pipeline,
        execute_stored_analysis_pipeline,
        get_analyze_db_collection,
        get_analyze_job,
        get_stored_scrapes_collection,
        load_analyze_cache,
        load_analyze_cache_from_db,
        run_analyze_job,
        save_analyze_cache,
        save_analyze_cache_to_db,
        serialize_analyze_job_payload,
        utc_now_iso,
    )
    from services.api_support import (
        configure_logfire_for_app,
        normalize_deadline_seconds,
        normalize_input_usernames,
        normalize_location_queries,
        normalize_reverse_location_points,
        verify_api_key,
    )
    from services.get_location import (
        resolve_location_requests as resolve_location_requests_service,
        resolve_reverse_location_requests as resolve_reverse_location_requests_service,
    )
    import services.google_search_headless as google_search_headless_service
    from services.instagram_profile_data_service import (
        resolve_instagram_profile_data_usernames as resolve_instagram_profile_data_usernames_service,
    )

logger = logging.getLogger(__name__)
search_instagram_users_service = google_search_headless_service.run_user_search
run_user_search = search_instagram_users_service

PROFILE_SCRAPE_TRIGGER_URL = os.getenv(
    "PROFILE_SCRAPE_TRIGGER_URL",
    "http://localhost:5000/profiles/scrape",
)
PROFILE_SCRAPE_TRIGGER_TEMPLATE = {
    "usernames": [],
    "max_posts": 24,
    "max_comments": 50,
    "post_workers": 2,
    "force_refresh": False,
    "cache_max_age_days": 7,
    "mongodb_database": "instagpy",
    "mongodb_collection": "instagram_scrapes",
    "save_image": False,
    "country":None,
    "city":None
}


load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")


app = FastAPI(
    title="BrightScraper API",
    version="2.0.0",
    description="Minimal FastAPI service for the active BrightScraper endpoints.",
)
app.state.analyze_jobs = {}
app.state.analyze_tasks = {}


configure_logfire_for_app(app)


def _extract_usernames_from_search_results(results: list[dict[str, Any]]) -> list[str]:
    usernames: list[str] = []
    seen: set[str] = set()

    for item in results:
        if not isinstance(item, dict):
            continue

        raw_username = item.get("username")
        if not isinstance(raw_username, str):
            continue

        username = raw_username.strip().lstrip("@").strip("/")
        if not username:
            continue

        normalized_key = username.lower()
        if normalized_key in seen:
            continue

        seen.add(normalized_key)
        usernames.append(username)

    return usernames


async def _post_profile_scrape_request(usernames: list[str], country=None, city=None) -> None:
    if not usernames:
        return

    payload = dict(PROFILE_SCRAPE_TRIGGER_TEMPLATE)
    payload["usernames"] = usernames
    payload["country"] = country
    payload["city"] = city

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                PROFILE_SCRAPE_TRIGGER_URL,
                headers={
                    "accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except Exception as exc:
        logger.warning(
            "Profile scrape trigger failed for usernames=%s: %s",
            usernames,
            exc,
        )


def _schedule_profile_scrape_trigger(usernames: list[str], country=None, city=None) -> None:
    if not usernames:
        return

    asyncio.create_task(_post_profile_scrape_request(usernames, country, city))


def _stored_source_kwargs(
    database_name: str | None,
    collection_name: str | None,
) -> dict[str, str]:
    kwargs: dict[str, str] = {}
    if isinstance(database_name, str) and database_name.strip():
        kwargs["database_name"] = database_name.strip()
    if isinstance(collection_name, str) and collection_name.strip():
        kwargs["collection_name"] = collection_name.strip()
    return kwargs


@app.get("/health")
async def health() -> dict[str, Any]:
    cleanup_expired_analyze_jobs(app.state)
    return {
        "status": "ok",
        "analyze_jobs_total": len(app.state.analyze_jobs),
        "analyze_jobs_active": len(app.state.analyze_tasks),
        "analyze_cache_dir": str(ANALYZE_CACHE_DIR),
        "instagram_profiles_data_collection": INSTAGRAM_PROFILES_DATA_COLLECTION,
        "instagram_profiles_data_connected": get_instagram_profiles_data_collection() is not None,
        "analyze_cache_db_collection": ANALYZE_CACHE_DB_COLLECTION,
        "analyze_cache_db_connected": get_analyze_db_collection() is not None,
        "stored_scrapes_db": STORED_SCRAPES_DB_NAME,
        "stored_scrapes_collection": STORED_SCRAPES_COLLECTION,
        "stored_scrapes_connected": get_stored_scrapes_collection() is not None,
    }


@app.get("/")
async def root() -> dict[str, Any]:
    return {"status": "ok"}


@app.post("/locations/resolve", dependencies=[Depends(verify_api_key)])
async def resolve_locations(payload: LocationLookupRequest) -> dict[str, Any]:
    location_queries = normalize_location_queries(payload.search_query, payload.search_queries)
    if not location_queries:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one search query via 'search_query' or 'search_queries'.",
        )

    try:
        return await resolve_location_requests_service(
            queries=location_queries,
            parallelism=payload.parallelism,
            limit=payload.limit,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Location lookup failed: {exc}",
        ) from exc


@app.post("/locations/reverse", dependencies=[Depends(verify_api_key)])
async def reverse_resolve_locations(payload: ReverseLocationLookupRequest) -> dict[str, Any]:
    location_points = normalize_reverse_location_points(payload.point, payload.points)
    if not location_points:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one point via 'point' or 'points'.",
        )

    try:
        return await resolve_reverse_location_requests_service(
            points=location_points,
            parallelism=payload.parallelism,
            zoom=payload.zoom,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Reverse location lookup failed: {exc}",
        ) from exc


async def _execute_profile_scrape_from_stored_data(
    username: str,
    request: ProfileScrapeRequest,
) -> tuple[dict[str, Any], int]:
    deadline_seconds = normalize_deadline_seconds(request.deadline_seconds)
    fast_mode = request.fast_mode if request.fast_mode is not None else False
    stored_source_kwargs = _stored_source_kwargs(
        request.mongodb_database,
        request.mongodb_collection,
    )

    envelope, http_status = await asyncio.to_thread(
        execute_stored_analysis_pipeline,
        username,
        deadline_seconds,
        fast_mode,
        **stored_source_kwargs,
    )

    warnings = envelope.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
    else:
        warnings = list(warnings)

    if request.force_refresh:
        warnings.append(
            "force_refresh was requested, but this endpoint analyzed the latest stored MongoDB scrape."
        )
    if request.max_posts is not None or request.max_comments is not None:
        warnings.append(
            "Stored-data analysis used all posts and comments available in the MongoDB document."
        )
    envelope["warnings"] = warnings

    envelope["cache"] = {
        "hit": False,
        "source": "stored_data",
        "age_seconds": 0,
        "ttl_seconds": None,
    }
    envelope["profile_scrape_request"] = {
        "username": username,
        "mongodb_database": stored_source_kwargs.get("database_name", STORED_SCRAPES_DB_NAME),
        "mongodb_collection": stored_source_kwargs.get("collection_name", STORED_SCRAPES_COLLECTION),
        "fast_mode": fast_mode,
        "deadline_seconds": deadline_seconds,
        "used_all_stored_posts_and_comments": True,
    }
    return envelope, http_status


@app.post("/profiles/scrape")
async def scrape_profiles_from_stored_data(payload: ProfileScrapeRequest) -> JSONResponse:
    usernames = normalize_input_usernames(payload.username, payload.usernames)
    if not usernames:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one username via 'username' or 'usernames'.",
        )

    if len(usernames) == 1:
        username = usernames[0]
        try:
            envelope, http_status = await _execute_profile_scrape_from_stored_data(username, payload)
        except Exception as exc:
            error_envelope, http_status = build_error_envelope(exc, username=username)
            return JSONResponse(status_code=http_status, content=error_envelope)

        return JSONResponse(status_code=http_status, content=envelope)

    results: dict[str, Any] = {}
    failed_usernames: list[str] = []
    max_http_status = status.HTTP_200_OK

    for username in usernames:
        try:
            envelope, http_status = await _execute_profile_scrape_from_stored_data(username, payload)
            results[username] = envelope
            max_http_status = max(max_http_status, http_status)
            if not envelope.get("success", False):
                failed_usernames.append(username)
        except Exception as exc:
            error_envelope, http_status = build_error_envelope(exc, username=username)
            results[username] = error_envelope
            failed_usernames.append(username)
            max_http_status = max(max_http_status, http_status)

    response_status = status.HTTP_200_OK if not failed_usernames else 207
    if failed_usernames and len(failed_usernames) == len(usernames):
        response_status = max_http_status

    return JSONResponse(
        status_code=response_status,
        content={
            "success": not failed_usernames,
            "status": "success" if not failed_usernames else "partial",
            "requested_usernames": usernames,
            "failed_usernames": failed_usernames,
            "results": results,
        },
    )


@app.post("/demographics/analyze", dependencies=[Depends(verify_api_key)])
async def analyze_audience(payload: DemographicsAnalyzeRequest | None = None) -> JSONResponse:
    request = payload or DemographicsAnalyzeRequest()
    username = normalize_instagram_username(request.username)

    if not username:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "success": False,
                "status": "failed",
                "error_code": "INPUT_INVALID",
                "error": "Username is required",
                "message": "Username is required",
                "warnings": [],
                "timings": {},
                "retry_summary": {},
            },
        )

    mode = request.mode.lower()
    fast_mode = request.fast_mode
    use_stored_data = request.use_stored_data
    deadline_seconds = normalize_deadline_seconds(request.deadline_seconds)
    default_max_posts = 4 if fast_mode else 6
    stored_source_kwargs = _stored_source_kwargs(
        request.mongodb_database,
        request.mongodb_collection,
    )

    max_posts = request.max_posts if request.max_posts is not None else default_max_posts

    if use_stored_data and mode != "async":
        try:
            envelope, http_status = await asyncio.to_thread(
                execute_stored_analysis_pipeline,
                username,
                deadline_seconds,
                fast_mode,
                **stored_source_kwargs,
            )
        except Exception as exc:
            error_envelope, http_status = build_error_envelope(exc, username=username)
            return JSONResponse(status_code=http_status, content=error_envelope)

        envelope["cache"] = {
            "hit": False,
            "source": "stored_data",
            "age_seconds": 0,
            "ttl_seconds": None,
        }
        return JSONResponse(status_code=http_status, content=envelope)

    if mode != "async":
        db_cached_payload, db_cache_age_seconds = load_analyze_cache_from_db(username)
        if db_cached_payload is not None:
            db_cached_payload["cache"] = {
                "hit": True,
                "source": "db",
                "age_seconds": db_cache_age_seconds,
                "ttl_seconds": ANALYZE_CACHE_TTL_SECONDS,
            }
            return JSONResponse(status_code=status.HTTP_200_OK, content=db_cached_payload)

        cached_payload, cache_age_seconds = load_analyze_cache(username)
        if cached_payload is not None:
            save_analyze_cache_to_db(username, cached_payload)
            cached_payload["cache"] = {
                "hit": True,
                "source": "file",
                "age_seconds": cache_age_seconds,
                "ttl_seconds": ANALYZE_CACHE_TTL_SECONDS,
            }
            return JSONResponse(status_code=status.HTTP_200_OK, content=cached_payload)

    if mode == "async":
        job = create_analyze_job(
            app.state,
            {
                "username": username,
                "mode": "async",
                "max_posts": max_posts,
                "deadline_seconds": deadline_seconds,
                "fast_mode": fast_mode,
                "use_stored_data": use_stored_data,
                "mongodb_database": stored_source_kwargs.get("database_name"),
                "mongodb_collection": stored_source_kwargs.get("collection_name"),
            }
        )
        task = asyncio.create_task(
            run_analyze_job(
                app.state,
                job["job_id"],
                username,
                max_posts,
                deadline_seconds,
                fast_mode,
                use_stored_data,
                stored_source_kwargs.get("database_name"),
                stored_source_kwargs.get("collection_name"),
            )
        )
        app.state.analyze_tasks[job["job_id"]] = task
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "success": True,
                "status": "queued",
                "error_code": None,
                "job_id": job["job_id"],
                "status_url": f"/analyze/jobs/{job['job_id']}",
                "eta_seconds": deadline_seconds,
                "warnings": [],
                "timings": {},
                "retry_summary": {},
            },
        )

    try:
        envelope, http_status = await asyncio.to_thread(
            execute_analysis_pipeline,
            username,
            max_posts,
            deadline_seconds,
            fast_mode,
        )
    except Exception as exc:
        error_envelope, http_status = build_error_envelope(exc, username=username)
        return JSONResponse(status_code=http_status, content=error_envelope)

    envelope["cache"] = {
        "hit": False,
        "source": "fresh",
        "age_seconds": 0,
        "ttl_seconds": ANALYZE_CACHE_TTL_SECONDS,
    }
    if save_analyze_cache_to_db(username, envelope):
        envelope["cache"]["stored_in_db"] = True
    analyze_cache_file = save_analyze_cache(username, envelope)
    if analyze_cache_file:
        envelope["cache"]["cache_file"] = analyze_cache_file

    return JSONResponse(status_code=http_status, content=envelope)


@app.get("/analyze/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def analyze_job_status(job_id: str) -> JSONResponse:
    job = get_analyze_job(app.state, job_id)
    if not job:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "success": False,
                "status": "failed",
                "error_code": "NO_DATA",
                "message": f"Job {job_id} not found or expired",
            },
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "success": True,
            **serialize_analyze_job_payload(job),
        },
    )


@app.post("/instagram/profile-data", dependencies=[Depends(verify_api_key)])
async def get_or_fetch_instagram_profile_data_bulk(
    payload: InstagramProfileDataRequest,
) -> dict[str, Any]:
    usernames = normalize_input_usernames(payload.username, payload.usernames)
    if not usernames:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one username via 'username' or 'usernames'.",
        )

    return await resolve_instagram_profile_data_usernames_service(
        usernames,
        refresh_stale=True,
        get_collection_fn=get_instagram_profiles_data_collection,
        load_from_db_fn=load_instagram_profile_data_from_db,
        fetch_and_store_fn=fetch_and_store_profile_data_blocking,
        collection_name=INSTAGRAM_PROFILES_DATA_COLLECTION,
    )


@app.post("/analytics", dependencies=[Depends(verify_api_key)])
async def analytics(payload: AnalyticsRequest) -> dict[str, Any]:
    usernames = normalize_input_usernames(payload.username, payload.usernames)
    if not usernames:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one username via 'username' or 'usernames'.",
        )

    resolved_payload = await resolve_instagram_profile_data_usernames_service(
        usernames,
        refresh_stale=False,
        get_collection_fn=get_instagram_profiles_data_collection,
        load_from_db_fn=load_instagram_profile_data_from_db,
        fetch_and_store_fn=fetch_and_store_profile_data_blocking,
        collection_name=INSTAGRAM_PROFILES_DATA_COLLECTION,
    )
    profiles = resolved_payload.get("profiles", {})

    analytics_result: dict[str, Any] = {}
    not_found_usernames: list[str] = []
    found_usernames: list[str] = []

    for username in usernames:
        profile_entry = profiles.get(username)
        if not isinstance(profile_entry, dict):
            not_found_usernames.append(username)
            continue

        profile_document = profile_entry.get("data")
        if not isinstance(profile_document, dict):
            not_found_usernames.append(username)
            continue

        analytics_result[username] = build_user_analytics(username, profile_document)
        found_usernames.append(username)

    return {
        "generated_at": utc_now_iso(),
        "requested_usernames": usernames,
        "found_usernames": found_usernames,
        "not_found_usernames": not_found_usernames,
        "db_usernames": resolved_payload.get("db_usernames", []),
        "fetched_usernames": resolved_payload.get("fetched_usernames", []),
        "analytics": analytics_result,
    }


@app.post("/instagram/search-users", dependencies=[Depends(verify_api_key)])
async def search_instagram_users(payload: SearchPayload) -> list[dict[str, Any]]:
    prompt = (payload.prompt or "").strip()
    if not prompt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="prompt is required",
        )

    try:
        search_service = run_user_search

        if asyncio.iscoroutinefunction(search_service):
            results = await search_service(query=prompt)
        else:
            results = await asyncio.to_thread(search_service, query=prompt)

        city=None
        country=None
        
        if len(results) > 0:
            city=results[0].get("city")
            country=results[0].get("country")
        
        
        _schedule_profile_scrape_trigger(
           usernames= _extract_usernames_from_search_results(results),
            country=country, city=city
        )
        return results
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Instagram user search failed for prompt=%r", prompt)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Instagram user search failed: {exc}",
        ) from exc


@app.post("/instagram/creator-search", dependencies=[Depends(verify_api_key)])
async def creator_search(payload: CreatorSearchRequest) -> dict[str, Any]:
    user_query = payload.user_query.strip()
    if not user_query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user_query is required",
        )

    if (
        payload.min_followers is not None
        and payload.max_followers is not None
        and payload.min_followers > payload.max_followers
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="min_followers must be less than or equal to max_followers",
        )

    niches = list(payload.niches)
    if payload.niche:
        niches.insert(0, payload.niche)

    try:
        result = await asyncio.to_thread(
            run_creator_search_agent,
            user_query,
            collection_name=payload.collection_name or payload.collection,
            country=payload.country,
            city=payload.city,
            niches=niches,
            min_followers=payload.min_followers,
            max_followers=payload.max_followers,
            limit=payload.limit,
        )
        return {
            "user_query": user_query,
            "result": result,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Creator search failed for user_query=%r", user_query)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Creator search failed: {exc}",
        ) from exc


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
