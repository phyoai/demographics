"""
Shared BrightData HTTP client with pooled connections, bounded retries,
and consistent snapshot polling.
"""

from __future__ import annotations

import os
import random
import time
from copy import deepcopy
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter


READY_STATUSES = {"ready", "success", "complete"}
PROCESSING_STATUSES = {"starting", "pending", "queued", "running", "processing"}
FAILED_STATUSES = {"failed", "error"}
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class BrightDataError(Exception):
    """Base BrightData client exception."""

    def __init__(self, message: str, retry_summary: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.retry_summary = retry_summary or {}


class BrightDataTimeoutError(BrightDataError):
    """Raised when a trigger/snapshot request exceeds retry/deadline limits."""


class BrightDataRateLimitError(BrightDataError):
    """Raised when BrightData repeatedly returns HTTP 429."""


class BrightDataBadResponseError(BrightDataError):
    """Raised when BrightData returns a malformed or non-retryable response."""


def new_retry_summary(profile: str = "balanced") -> Dict[str, Any]:
    """Create a retry summary object for one analysis run."""
    return {
        "profile": profile,
        "total_http_attempts": 0,
        "total_http_retries": 0,
        "rate_limit_hits": 0,
        "network_errors": 0,
        "retryable_status_hits": {},
        "operations": {},
        "last_error": None,
        "last_status": None,
    }


def merge_retry_summaries(base: Optional[Dict[str, Any]], extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge two retry summary dicts."""
    result = deepcopy(base or new_retry_summary())
    incoming = extra or {}

    result["total_http_attempts"] = result.get("total_http_attempts", 0) + incoming.get("total_http_attempts", 0)
    result["total_http_retries"] = result.get("total_http_retries", 0) + incoming.get("total_http_retries", 0)
    result["rate_limit_hits"] = result.get("rate_limit_hits", 0) + incoming.get("rate_limit_hits", 0)
    result["network_errors"] = result.get("network_errors", 0) + incoming.get("network_errors", 0)

    retryable_hits = result.setdefault("retryable_status_hits", {})
    for status_code, count in incoming.get("retryable_status_hits", {}).items():
        key = str(status_code)
        retryable_hits[key] = retryable_hits.get(key, 0) + count

    operations = result.setdefault("operations", {})
    for op_name, op_data in incoming.get("operations", {}).items():
        slot = operations.setdefault(
            op_name,
            {
                "attempts": 0,
                "retries": 0,
                "last_status": None,
                "last_error": None,
                "poll_iterations": 0,
            },
        )
        slot["attempts"] += op_data.get("attempts", 0)
        slot["retries"] += op_data.get("retries", 0)
        slot["poll_iterations"] += op_data.get("poll_iterations", 0)
        if op_data.get("last_status") is not None:
            slot["last_status"] = op_data.get("last_status")
        if op_data.get("last_error"):
            slot["last_error"] = op_data.get("last_error")

    if incoming.get("last_error"):
        result["last_error"] = incoming.get("last_error")
    if incoming.get("last_status") is not None:
        result["last_status"] = incoming.get("last_status")

    return result


class BrightDataClient:
    """HTTP client wrapper for BrightData trigger + snapshot polling."""

    RETRY_PROFILES = {
        "minimal": {
            "max_attempts": 3,
            "base_delay": 0.35,
            "max_delay": 1.5,
            "poll_retries": 8,
            "poll_delay": 1.25,
            "connect_timeout": 5,
            "read_timeout": 15,
        },
        "balanced": {
            "max_attempts": 5,
            "base_delay": 0.5,
            "max_delay": 4.0,
            "poll_retries": 18,
            "poll_delay": 2.0,
            "connect_timeout": 6,
            "read_timeout": 20,
        },
        "aggressive": {
            "max_attempts": 7,
            "base_delay": 0.75,
            "max_delay": 8.0,
            "poll_retries": 25,
            "poll_delay": 2.5,
            "connect_timeout": 8,
            "read_timeout": 25,
        },
    }

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        retry_profile: str = "balanced",
        pool_size: int = 20,
    ) -> None:
        self.api_key = api_key or os.getenv("BRIGHTDATA_API_KEY")
        self.base_url = base_url or os.getenv("BRIGHTDATA_BASE_URL", "https://api.brightdata.com/datasets/v3")
        self.retry_profile = retry_profile if retry_profile in self.RETRY_PROFILES else "balanced"
        self._cfg = self.RETRY_PROFILES[self.retry_profile]

        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise BrightDataBadResponseError("BrightData API key not configured")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _op_slot(summary: Dict[str, Any], operation: str) -> Dict[str, Any]:
        operations = summary.setdefault("operations", {})
        return operations.setdefault(
            operation,
            {
                "attempts": 0,
                "retries": 0,
                "last_status": None,
                "last_error": None,
                "poll_iterations": 0,
            },
        )

    @staticmethod
    def _check_deadline(deadline_at: Optional[float], summary: Dict[str, Any], operation: str) -> None:
        if deadline_at is None:
            return
        if time.monotonic() > deadline_at:
            slot = BrightDataClient._op_slot(summary, operation)
            slot["last_error"] = "deadline_exceeded"
            summary["last_error"] = "deadline_exceeded"
            raise BrightDataTimeoutError("Deadline exceeded while waiting for BrightData response", retry_summary=summary)

    @staticmethod
    def _sleep(seconds: float, deadline_at: Optional[float], summary: Dict[str, Any], operation: str) -> None:
        if seconds <= 0:
            return
        if deadline_at is None:
            time.sleep(seconds)
            return
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            BrightDataClient._check_deadline(deadline_at, summary, operation)
            return
        time.sleep(min(seconds, remaining))
        BrightDataClient._check_deadline(deadline_at, summary, operation)

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        payload: Optional[Any] = None,
        summary: Optional[Dict[str, Any]] = None,
        deadline_at: Optional[float] = None,
        timeout: Optional[tuple[int, int]] = None,
        max_attempts: Optional[int] = None,
    ) -> Any:
        tracker = summary if summary is not None else new_retry_summary(self.retry_profile)
        max_attempts = max_attempts or self._cfg["max_attempts"]
        timeout = timeout or (self._cfg["connect_timeout"], self._cfg["read_timeout"])
        slot = self._op_slot(tracker, operation)

        for attempt in range(1, max_attempts + 1):
            self._check_deadline(deadline_at, tracker, operation)
            tracker["total_http_attempts"] += 1
            slot["attempts"] += 1
            if attempt > 1:
                tracker["total_http_retries"] += 1
                slot["retries"] += 1

            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=self._headers(),
                    json=payload,
                    timeout=timeout,
                )
                status = response.status_code
                slot["last_status"] = status
                tracker["last_status"] = status

                if 200 <= status < 300:
                    try:
                        return response.json()
                    except ValueError as exc:
                        slot["last_error"] = "invalid_json"
                        tracker["last_error"] = "invalid_json"
                        raise BrightDataBadResponseError(
                            f"BrightData returned invalid JSON for {operation}",
                            retry_summary=tracker,
                        ) from exc

                if status in RETRYABLE_STATUS_CODES and attempt < max_attempts:
                    key = str(status)
                    hits = tracker.setdefault("retryable_status_hits", {})
                    hits[key] = hits.get(key, 0) + 1
                    if status == 429:
                        tracker["rate_limit_hits"] += 1
                    delay = min(self._cfg["base_delay"] * (2 ** (attempt - 1)), self._cfg["max_delay"])
                    delay *= random.uniform(0.8, 1.2)
                    self._sleep(delay, deadline_at, tracker, operation)
                    continue

                if status == 429:
                    slot["last_error"] = "rate_limited"
                    tracker["last_error"] = "rate_limited"
                    raise BrightDataRateLimitError(
                        f"BrightData rate limit reached (HTTP {status}) for {operation}",
                        retry_summary=tracker,
                    )

                slot["last_error"] = f"http_{status}"
                tracker["last_error"] = f"http_{status}"
                raise BrightDataBadResponseError(
                    f"BrightData returned HTTP {status} for {operation}: {response.text[:250]}",
                    retry_summary=tracker,
                )

            except requests.exceptions.Timeout as exc:
                tracker["network_errors"] += 1
                slot["last_error"] = "timeout"
                tracker["last_error"] = "timeout"
                if attempt >= max_attempts:
                    raise BrightDataTimeoutError(
                        f"BrightData timeout during {operation}",
                        retry_summary=tracker,
                    ) from exc
                delay = min(self._cfg["base_delay"] * (2 ** (attempt - 1)), self._cfg["max_delay"])
                delay *= random.uniform(0.8, 1.2)
                self._sleep(delay, deadline_at, tracker, operation)

            except requests.exceptions.RequestException as exc:
                tracker["network_errors"] += 1
                slot["last_error"] = "network_error"
                tracker["last_error"] = "network_error"
                if attempt >= max_attempts:
                    raise BrightDataBadResponseError(
                        f"BrightData request failed during {operation}: {exc}",
                        retry_summary=tracker,
                    ) from exc
                delay = min(self._cfg["base_delay"] * (2 ** (attempt - 1)), self._cfg["max_delay"])
                delay *= random.uniform(0.8, 1.2)
                self._sleep(delay, deadline_at, tracker, operation)

        raise BrightDataTimeoutError(
            f"BrightData request exhausted retries for {operation}",
            retry_summary=tracker,
        )

    def trigger_snapshot(
        self,
        *,
        dataset_id: str,
        payload: Any,
        include_errors: bool = True,
        extra_query: Optional[Dict[str, str]] = None,
        summary: Optional[Dict[str, Any]] = None,
        deadline_at: Optional[float] = None,
        operation: str = "trigger_snapshot",
    ) -> str:
        query = {"dataset_id": dataset_id}
        if include_errors:
            query["include_errors"] = "true"
        if extra_query:
            for key, value in extra_query.items():
                if value is not None:
                    query[key] = str(value)

        trigger_url = f"{self.base_url}/trigger?{urlencode(query)}"
        data = self._request_json(
            "POST",
            trigger_url,
            operation=operation,
            payload=payload,
            summary=summary,
            deadline_at=deadline_at,
        )

        snapshot_id = data.get("snapshot_id") if isinstance(data, dict) else None
        if not snapshot_id:
            tracker = summary if summary is not None else new_retry_summary(self.retry_profile)
            tracker["last_error"] = "missing_snapshot_id"
            raise BrightDataBadResponseError(
                f"BrightData trigger missing snapshot_id for {operation}: {data}",
                retry_summary=tracker,
            )
        return snapshot_id

    def fetch_snapshot_data(
        self,
        snapshot_id: str,
        *,
        summary: Optional[Dict[str, Any]] = None,
        deadline_at: Optional[float] = None,
        poll_retries: Optional[int] = None,
        poll_delay: Optional[float] = None,
        operation: str = "snapshot_poll",
    ) -> Any:
        if not snapshot_id:
            raise BrightDataBadResponseError("Snapshot ID is required")

        tracker = summary if summary is not None else new_retry_summary(self.retry_profile)
        poll_retries = poll_retries or self._cfg["poll_retries"]
        poll_delay = self._cfg["poll_delay"] if poll_delay is None else poll_delay
        slot = self._op_slot(tracker, operation)

        snapshot_url = f"{self.base_url}/snapshot/{snapshot_id}?format=json"

        # Initial short delay improves chance of first successful poll.
        self._sleep(0.6, deadline_at, tracker, operation)

        for poll_index in range(1, poll_retries + 1):
            self._check_deadline(deadline_at, tracker, operation)
            slot["poll_iterations"] += 1

            data = self._request_json(
                "GET",
                snapshot_url,
                operation=operation,
                summary=tracker,
                deadline_at=deadline_at,
            )

            if isinstance(data, list):
                return data

            if not isinstance(data, dict):
                tracker["last_error"] = "invalid_snapshot_payload"
                raise BrightDataBadResponseError(
                    f"Unexpected snapshot payload type for {snapshot_id}: {type(data).__name__}",
                    retry_summary=tracker,
                )

            status = str(data.get("status", "unknown")).lower()
            slot["last_status"] = status
            tracker["last_status"] = status

            if status in READY_STATUSES:
                result_data = data.get("data")
                if isinstance(result_data, list):
                    return result_data
                if isinstance(data, list):
                    return data
                return []

            if status in PROCESSING_STATUSES:
                if poll_index >= poll_retries:
                    break
                delay = poll_delay * random.uniform(0.85, 1.15)
                self._sleep(delay, deadline_at, tracker, operation)
                continue

            if status in FAILED_STATUSES:
                error_msg = data.get("error", "BrightData snapshot failed")
                tracker["last_error"] = str(error_msg)
                raise BrightDataBadResponseError(
                    f"Snapshot {snapshot_id} failed: {error_msg}",
                    retry_summary=tracker,
                )

            tracker["last_error"] = f"unexpected_status:{status}"
            raise BrightDataBadResponseError(
                f"Unexpected BrightData snapshot status '{status}' for snapshot {snapshot_id}",
                retry_summary=tracker,
            )

        tracker["last_error"] = "snapshot_poll_timeout"
        raise BrightDataTimeoutError(
            f"Snapshot {snapshot_id} not ready after {poll_retries} polls",
            retry_summary=tracker,
        )
