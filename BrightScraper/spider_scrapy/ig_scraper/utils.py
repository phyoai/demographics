from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import urlparse


COMPACT_NUMBER_RE = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*([kmbt]?)", re.IGNORECASE)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def parse_compact_number(value: Any) -> int | None:
    text = normalize_text(value)
    if not text:
        return None

    compact = text.replace(",", "")
    match = COMPACT_NUMBER_RE.search(compact)
    if not match:
        return None

    number_part = match.group(1).replace(",", "")
    suffix = match.group(2).lower()
    multipliers = {
        "": 1,
        "k": 1_000,
        "m": 1_000_000,
        "b": 1_000_000_000,
        "t": 1_000_000_000_000,
    }

    try:
        number = float(number_part)
    except ValueError:
        return None
    return int(number * multipliers.get(suffix, 1))


def to_iso_datetime(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(microsecond=0).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    text = normalize_text(value)
    if not text:
        return None

    if text.isdigit():
        return to_iso_datetime(int(text))

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        dt = None

    if dt is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=0).isoformat()


def canonicalize_instagram_url(url_or_path: str | None) -> str | None:
    text = normalize_text(url_or_path)
    if not text:
        return None

    if text.startswith("//"):
        text = "https:" + text
    elif text.startswith("/"):
        text = "https://www.instagram.com" + text
    elif not text.startswith("http://") and not text.startswith("https://"):
        text = "https://www.instagram.com/" + text.lstrip("/")

    parsed = urlparse(text)
    cleaned = f"https://www.instagram.com{parsed.path}"
    if not cleaned.endswith("/"):
        cleaned += "/"
    return cleaned


def extract_shortcode_from_url(url: str | None) -> str | None:
    text = normalize_text(url)
    if not text:
        return None

    path_parts = [segment for segment in urlparse(text).path.split("/") if segment]
    if len(path_parts) < 2:
        return None

    if path_parts[0] in {"p", "reel", "tv"}:
        return path_parts[1]
    return None


def safe_json_loads(text: str | None) -> Any:
    if text is None:
        return None
    candidate = str(text).strip()
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except (TypeError, json.JSONDecodeError):
        return None


def _extract_balanced_json(text: str, start_index: int) -> str | None:
    if start_index < 0 or start_index >= len(text):
        return None

    opener = text[start_index]
    if opener not in "{[":
        return None
    closer = "}" if opener == "{" else "]"

    depth = 0
    in_string = False
    escape = False

    for index in range(start_index, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start_index : index + 1]
    return None


def _stable_dump(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


def extract_json_objects_from_script_text(script_text: str | None, max_objects: int = 8) -> list[Any]:
    if not script_text:
        return []

    text = script_text.strip()
    if not text:
        return []

    objects: list[Any] = []
    seen: set[str] = set()

    direct = safe_json_loads(text)
    if direct is not None:
        marker = _stable_dump(direct)
        if marker not in seen:
            seen.add(marker)
            objects.append(direct)
        return objects

    markers = ["window._sharedData", "window.__additionalDataLoaded", "window.__initialDataLoaded"]
    for marker in markers:
        marker_index = text.find(marker)
        if marker_index == -1:
            continue
        brace_index = text.find("{", marker_index)
        if brace_index == -1:
            continue
        blob = _extract_balanced_json(text, brace_index)
        parsed = safe_json_loads(blob)
        if parsed is None:
            continue
        marker_dump = _stable_dump(parsed)
        if marker_dump not in seen:
            seen.add(marker_dump)
            objects.append(parsed)
        if len(objects) >= max_objects:
            return objects

    starts = [match.start() for match in re.finditer(r"[{\[]", text)]
    for start_index in starts[:25]:
        blob = _extract_balanced_json(text, start_index)
        parsed = safe_json_loads(blob)
        if parsed is None:
            continue
        marker_dump = _stable_dump(parsed)
        if marker_dump in seen:
            continue
        seen.add(marker_dump)
        objects.append(parsed)
        if len(objects) >= max_objects:
            break

    return objects


def extract_json_objects_from_scripts(script_texts: Iterable[str]) -> list[Any]:
    objects: list[Any] = []
    for script_text in script_texts:
        objects.extend(extract_json_objects_from_script_text(script_text))
    return objects


def iter_dicts(data: Any) -> Iterator[dict[str, Any]]:
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from iter_dicts(value)
    elif isinstance(data, list):
        for item in data:
            yield from iter_dicts(item)


def deep_find_values(data: Any, key: str) -> list[Any]:
    values: list[Any] = []
    if isinstance(data, dict):
        for dict_key, value in data.items():
            if dict_key == key:
                values.append(value)
            values.extend(deep_find_values(value, key))
    elif isinstance(data, list):
        for item in data:
            values.extend(deep_find_values(item, key))
    return values


def get_nested_value(data: Any, path: str | Sequence[Any], default: Any = None) -> Any:
    parts: Sequence[Any]
    if isinstance(path, str):
        parts = path.split(".")
    else:
        parts = path

    current = data
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return default
            current = current[part]
            continue
        if isinstance(current, list):
            try:
                index = int(part)
            except (TypeError, ValueError):
                return default
            if index < 0 or index >= len(current):
                return default
            current = current[index]
            continue
        return default
    return current


def unique_preserve_order(values: Iterable[Any]) -> list[Any]:
    output: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = normalize_text(value)
        if marker is None or marker in seen:
            continue
        seen.add(marker)
        output.append(value)
    return output


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(
        dir=str(output_path.parent),
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        text=True,
    )

    try:
        with os.fdopen(fd, mode="w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, output_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
