from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UsernamesRequest(StrictModel):
    username: str | None = None
    usernames: list[str] = Field(default_factory=list)


class AnalyticsRequest(UsernamesRequest):
    pass


class InstagramProfileDataRequest(UsernamesRequest):
    pass


class DemographicsAnalyzeRequest(StrictModel):
    username: str | None = None
    mode: str = "sync"
    fast_mode: bool = True
    use_stored_data: bool = False
    deadline_seconds: int | None = None
    max_posts: int | None = Field(default=None, ge=1, le=40)


class SearchPayload(StrictModel):
    prompt: str | None = None


class CreatorSearchRequest(StrictModel):
    user_query: str
    collection: str | None = None
    collection_name: str | None = None
    country: str | None = None
    city: str | None = None
    niche: str | None = None
    niches: list[str] = Field(default_factory=list)
    min_followers: int | None = Field(default=None, ge=0)
    max_followers: int | None = Field(default=None, ge=0)
    limit: int | None = Field(default=10, ge=1, le=100)


class LocationLookupRequest(StrictModel):
    search_query: str | None = None
    search_queries: list[str] = Field(default_factory=list)
    parallelism: int = Field(default=4, ge=1, le=50)
    limit: int = Field(default=1, ge=1, le=10)


class ReverseLocationPoint(StrictModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class ReverseLocationLookupRequest(StrictModel):
    point: ReverseLocationPoint | None = None
    points: list[ReverseLocationPoint] = Field(default_factory=list)
    parallelism: int = Field(default=4, ge=1, le=50)
    zoom: int = Field(default=15, ge=0, le=18)
