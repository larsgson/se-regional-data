#!/usr/bin/env python3
"""
Extract per-language video and audio manifests from each SE deployment's
large "contents" JS chunk, and write them into data/pkf/<iso>/info.json
under `media`. Nothing is stored locally — all references are absolute URLs
to scriptureearth.org / youtube.com / 4.dbt.io, rendered on demand.

Structure stored:
    media.audio = {
      base_url: "https://www.scriptureearth.org/data/<iso>/audio",
      items: [ { bookCode, chapter, filename, url, len, size, timingFile,
                 hasTiming } ]
    }
    media.videos = [
      { id, title, width, height, thumbnail, thumbnailUrl, onlineUrl,
        kind: 'youtube' | 'hls' | 'other',
        placement: { bookCode, chapter, verse, pos } }
    ]
    timing files: data/pkf/<iso>/timing/<BOOK>-<chapter>.json
      each is [[start, end, tag], ...] parsed from SE's TSV timing data.
      Auto-generated timing is decoded from base64 data URIs in the JS chunk;
      manual overrides are fetched from hashed asset files.

Idempotent. Safe to re-run after SE redeploys (JS-chunk hashes change, so
we always re-discover the manifest chunk via grep).

Env:
    CONCURRENCY=6   number of parallel workers (default 6)
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import REPO_ROOT  # noqa: E402

SE = "https://scriptureearth.org"
PKF_ROOT = REPO_ROOT / "data" / "pkf"

UA = "bw-map-media/1.0"

SW_IMG_RE = re.compile(
    r"/_app/immutable/assets/([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\.(jpg|jpeg|png|webp)",
    re.I,
)
CHUNK_IN_SW_RE = re.compile(
    r's\+"(/_app/immutable/(?:chunks|nodes)/[A-Za-z0-9_.\-]+\.js)"'
)


# --- low-level utilities ----------------------------------------------------


def fetch_text(url: str) -> Optional[str]:
    try:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError):
        return None


def head_size(url: str) -> int:
    try:
        req = Request(url, headers={"User-Agent": UA}, method="HEAD")
        with urlopen(req, timeout=15) as r:
            cl = r.headers.get("content-length")
            return int(cl) if cl else 0
    except (HTTPError, URLError, TimeoutError):
        return 0


def find_matching_brace(text: str, start_idx: int) -> int:
    """From text[start_idx] == '{', return index of the matching '}', respecting
    JS strings so braces inside strings aren't counted. Returns -1 if no match."""
    depth = 0
    in_str = False
    string_char: Optional[str] = None
    i = start_idx
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == string_char:
                in_str = False
            i += 1
            continue
        if c in ('"', "'", "`"):
            in_str = True
            string_char = c
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def collect_objects_by_handle(text: str, handle_re: re.Pattern) -> list[str]:
    """For the first regex match, walk back to find the enclosing '{' and
    forward to find the matching '}'. Return each enclosing object substring."""
    out: list[str] = []
    for m in handle_re.finditer(text):
        # Walk backward to find the '{' that starts the enclosing object.
        idx = m.start()
        depth = 0
        while idx > 0:
            c = text[idx]
            if c == "}":
                depth += 1
            elif c == "{":
                if depth == 0:
                    break
                depth -= 1
            idx -= 1
        if idx < 0 or text[idx] != "{":
            continue
        end = find_matching_brace(text, idx)
        if end > idx:
            out.append(text[idx : end + 1])
    return out


def _decode_js_string(raw: str) -> str:
    """Decode a minified-JS-emitted string literal. Accepts "..." or '...'.
    Handles \\' (minifier artifact for ' inside double-quoted strings)."""
    if raw.startswith(('"', "'")):
        normalized = raw.replace("\\'", "'")
        try:
            return json.loads(normalized)
        except json.JSONDecodeError:
            # Fallback: try converting single-quoted to double-quoted.
            if normalized.startswith("'") and normalized.endswith("'"):
                inner = normalized[1:-1].replace('"', '\\"')
                return json.loads(f'"{inner}"')
            raise
    return raw


def get_field(obj_text: str, key: str):
    """Return the value of `key` (string or number) or None."""
    # Keys may be bare (id:) or quoted ("id":). Values: string or number only.
    pat = re.compile(
        r"(?:^|[{,])\s*[\"']?" + re.escape(key) + r"[\"']?\s*:\s*"
        r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|-?\d+(?:\.\d+)?)"
    )
    m = pat.search(obj_text)
    if not m:
        return None
    raw = m.group(1)
    if raw.startswith(('"', "'")):
        return _decode_js_string(raw)
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return None


def get_nested_object(obj_text: str, key: str) -> Optional[str]:
    """Find a nested object for `key` and return the substring including braces."""
    pat = re.compile(r"(?:^|[{,])\s*[\"']?" + re.escape(key) + r"[\"']?\s*:\s*\{")
    m = pat.search(obj_text)
    if not m:
        return None
    open_idx = m.end() - 1
    end = find_matching_brace(obj_text, open_idx)
    if end < 0:
        return None
    return obj_text[open_idx : end + 1]


# --- video extraction -------------------------------------------------------


def detect_video_kind(url: str) -> str:
    if not url:
        return "other"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if ".m3u8" in url:
        return "hls"
    if "arclight.org" in url:
        return "arclight"
    if "vimeo.com" in url:
        return "vimeo"
    if re.search(r"\.(mp4|webm|ogv|m4v|mov)(\?|$)", url, re.I):
        return "file"
    return "other"


def decode_html_entities(s: Optional[str]) -> Optional[str]:
    """Decode the small handful of HTML entities SE actually emits."""
    if not s:
        return s
    return (
        s.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )


def parse_placement_ref(ref: Optional[str]) -> dict:
    if not ref:
        return {}
    m = re.match(r"^([A-Z1-3]{2,4})[\s.:](\d+)(?:[.:](\d+))?", ref)
    if not m:
        return {"rawRef": ref}
    return {
        "bookCode": m.group(1),
        "chapter": int(m.group(2)),
        "verse": int(m.group(3)) if m.group(3) is not None else None,
    }


ONLINE_URL_RE = re.compile(r"\bonlineUrl\s*:")


def extract_videos(chunk_text: str, image_url_map: dict[str, str]) -> list[dict]:
    objs = collect_objects_by_handle(chunk_text, ONLINE_URL_RE)
    out: list[dict] = []
    seen: set[str] = set()
    for obj in objs:
        video_id = get_field(obj, "id")
        online_url = get_field(obj, "onlineUrl")
        if not video_id or not online_url:
            continue
        key = f"{video_id}|{online_url}"
        if key in seen:
            continue
        seen.add(key)

        thumbnail = get_field(obj, "thumbnail")
        placement_text = get_nested_object(obj, "placement")
        placement: dict = {}
        if placement_text:
            placement = {
                **parse_placement_ref(get_field(placement_text, "ref")),
                "pos": get_field(placement_text, "pos"),
                "collection": get_field(placement_text, "collection"),
            }

        decoded = decode_html_entities(online_url) or ""
        out.append(
            {
                "id": video_id,
                "title": get_field(obj, "title") or "",
                "width": get_field(obj, "width"),
                "height": get_field(obj, "height"),
                "thumbnail": thumbnail,
                "thumbnailUrl": image_url_map.get(thumbnail) if thumbnail else None,
                "onlineUrl": decoded,
                "kind": detect_video_kind(decoded),
                "placement": placement,
            }
        )
    return out


# --- audio extraction -------------------------------------------------------


SOURCES_HEAD_RE = re.compile(
    r"\b(?:audio)?[Ss]ources\s*:\s*\{\s*[A-Za-z][A-Za-z0-9_]*\s*:\s*\{\s*type\s*:\s*\"(?:download|streaming)\""
)


def extract_audio_sources(chunk_text: str) -> dict[str, str]:
    m = SOURCES_HEAD_RE.search(chunk_text)
    if not m:
        return {}
    open_idx = chunk_text.find("{", m.start())
    end = find_matching_brace(chunk_text, open_idx)
    if end < 0:
        return {}
    inner = chunk_text[open_idx : end + 1]

    sources: dict[str, str] = {}
    key_re = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s*:\s*\{")
    pos = 0
    while True:
        km = key_re.search(inner, pos)
        if not km:
            break
        k = km.group(1)
        start = km.end() - 1
        e = find_matching_brace(inner, start)
        if e < 0:
            break
        body = inner[start : e + 1]
        address = get_field(body, "address")
        if address:
            sources[k] = re.sub(r"^http://", "https://", address)
        pos = e + 1
    return sources


AUDIO_ITEM_HANDLE = re.compile(r'\bnum\s*:\s*\d+\s*,\s*filename\s*:\s*"[^"]+\.mp3"')
AUDIO_FILENAME_RE = re.compile(r"-(\d{2})-([A-Z0-9]{3})(?:-(\d+))?\.mp3$", re.I)


def parse_audio_filename(filename: str) -> dict:
    m = AUDIO_FILENAME_RE.search(filename)
    if not m:
        return {}
    chapter = int(m.group(3)) if m.group(3) else 1
    return {"bookCode": m.group(2).upper(), "chapter": chapter}


def extract_audio_items(chunk_text: str, sources: dict[str, str]) -> list[dict]:
    objs = collect_objects_by_handle(chunk_text, AUDIO_ITEM_HANDLE)
    seen: set[str] = set()
    out: list[dict] = []
    for obj in objs:
        filename = get_field(obj, "filename")
        if not filename or not filename.endswith(".mp3"):
            continue
        if filename in seen:
            continue
        seen.add(filename)
        src = get_field(obj, "src") or ""
        base_url = sources.get(src)
        url = f"{base_url}/{filename}" if base_url else None
        parsed = parse_audio_filename(filename)
        out.append(
            {
                "filename": filename,
                "url": url,
                "bookCode": parsed.get("bookCode"),
                "chapter": parsed.get("chapter"),
                "num": get_field(obj, "num"),
                "len": get_field(obj, "len"),
                "size": get_field(obj, "size"),
                "timingFile": get_field(obj, "timingFile"),
                "src": src,
            }
        )
    return out


# --- timing extraction --------------------------------------------------------


TIMING_FILENAME_RE = re.compile(r"-([A-Z0-9]{3})-(\d+)-timing\.txt$", re.I)

TIMING_ASSET_ENTRY_RE = re.compile(
    r'"\./([^"]*-timing\.txt)"\s*:\s*([A-Za-z_$][\w$]*)'
)
SW_TIMING_RE = re.compile(
    r"/_app/immutable/assets/([\w-]+-timing)\.([A-Za-z0-9_-]+)\.txt"
)


def parse_timing_tsv(text: str) -> list[list]:
    rows: list[list] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("\\"):
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            try:
                rows.append([round(float(parts[0]), 3), round(float(parts[1]), 3), parts[2]])
            except ValueError:
                continue
    return rows


def timing_urls_from_sw(iso: str, sw_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in SW_TIMING_RE.finditer(sw_text):
        out[m.group(1)] = f"{SE}/data/{iso}/sab/{iso}{m.group(0)}"
    return out


def extract_timing_data(
    chunk_text: str, iso: str, sw_text: str, audio_items: list[dict]
) -> dict[str, list]:
    """Extract timing data for audio items from the JS chunk.

    Auto-generated timing is embedded as base64 data URIs.
    Manual overrides are separate hashed .txt assets fetched via SW URLs.
    Returns {timingFile_name: [[start, end, tag], ...], ...}.
    """
    timing_names = {item["timingFile"] for item in audio_items if item.get("timingFile")}
    if not timing_names:
        return {}

    sw_urls = timing_urls_from_sw(iso, sw_text)

    file_to_var: dict[str, str] = {}
    for m in TIMING_ASSET_ENTRY_RE.finditer(chunk_text):
        if m.group(1) in timing_names:
            file_to_var[m.group(1)] = m.group(2)

    result: dict[str, list] = {}
    for fname, var in file_to_var.items():
        tsv_text = None
        varpat = r"(?<![A-Za-z0-9_$])" + re.escape(var)

        # Try direct string assignment: VAR="data:text/plain;base64,..."
        m = re.search(varpat + r'\s*=\s*"([^"]+)"', chunk_text)
        if m:
            value = m.group(1)
            if value.startswith("data:text/plain;base64,"):
                b64 = value[len("data:text/plain;base64,"):]
                try:
                    tsv_text = base64.b64decode(b64).decode("utf-8", errors="replace")
                except Exception:
                    pass
            elif value.startswith("/"):
                tsv_text = fetch_text(f"{SE}/data/{iso}/sab/{iso}{value}")

        # Manual overrides: VAR=""+new URL("../assets/NAME.HASH.txt",...)
        if tsv_text is None:
            m = re.search(
                varpat + r'\s*=\s*""\+new\s+URL\("(\.\./assets/[^"]+)"',
                chunk_text,
            )
            if m:
                asset = m.group(1).replace("../assets/", "/_app/immutable/assets/")
                tsv_text = fetch_text(f"{SE}/data/{iso}/sab/{iso}{asset}")

        # Fallback: try SW hashed URL
        if tsv_text is None:
            basename = fname.rsplit(".", 1)[0]
            url = sw_urls.get(basename)
            if url:
                tsv_text = fetch_text(url)

        if tsv_text:
            parsed = parse_timing_tsv(tsv_text)
            if parsed:
                result[fname] = parsed

    return result


# --- service-worker helpers -------------------------------------------------


def image_url_map_from(iso: str, sw_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in SW_IMG_RE.finditer(sw_text):
        base, h, ext = m.group(1), m.group(2), m.group(3)
        out[f"{base}.{ext}"] = f"{SE}/data/{iso}/sab/{iso}/_app/immutable/assets/{base}.{h}.{ext}"
    return out


def chunk_urls_from(sw_text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in CHUNK_IN_SW_RE.finditer(sw_text):
        if m.group(1) in seen:
            continue
        seen.add(m.group(1))
        out.append(m.group(1))
    return out


# --- main manifest-finder ---------------------------------------------------


def find_manifest_chunk(iso: str, chunk_paths: list[str]) -> Optional[str]:
    """HEAD each chunk to get content-length, then try largest-first. The
    contents manifest is typically the largest chunk by a wide margin."""
    with_sizes: list[dict] = []
    for p in chunk_paths:
        url = f"{SE}/data/{iso}/sab/{iso}{p}"
        with_sizes.append({"url": url, "size": head_size(url)})
    with_sizes.sort(key=lambda x: -x["size"])

    for entry in with_sizes[: min(12, len(with_sizes))]:
        body = fetch_text(entry["url"])
        if not body:
            continue
        if "onlineUrl:" in body or "audioSources:" in body:
            return body
    return None


def process_iso(iso: str) -> Optional[dict]:
    iso_dir = PKF_ROOT / iso
    info_path = iso_dir / "info.json"
    if not iso_dir.is_dir():
        return None
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    sw = fetch_text(f"{SE}/data/{iso}/sab/{iso}/service-worker.js")
    if not sw:
        return {"iso": iso, "error": "no SW"}

    image_map = image_url_map_from(iso, sw)
    chunks = chunk_urls_from(sw)
    if not chunks:
        return {"iso": iso, "videos": 0, "audio": 0}

    manifest_text = find_manifest_chunk(iso, chunks)
    if not manifest_text:
        return {"iso": iso, "videos": 0, "audio": 0}

    videos = extract_videos(manifest_text, image_map)
    sources = extract_audio_sources(manifest_text)
    audio_items = extract_audio_items(manifest_text, sources)

    if not videos and not audio_items:
        if "media" in info:
            del info["media"]
            info_path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"iso": iso, "videos": 0, "audio": 0}

    # Extract and write timing files
    timing_count = 0
    if audio_items:
        timing_data = extract_timing_data(manifest_text, iso, sw, audio_items)
        if timing_data:
            timing_dir = iso_dir / "timing"
            timing_dir.mkdir(exist_ok=True)
            for item in audio_items:
                tf = item.get("timingFile")
                book = item.get("bookCode")
                chapter = item.get("chapter")
                if not book or chapter is None:
                    tm = TIMING_FILENAME_RE.search(tf or "")
                    if tm:
                        book = tm.group(1).upper()
                        chapter = int(tm.group(2))
                if tf and tf in timing_data and book and chapter is not None:
                    out_path = timing_dir / f"{book}-{chapter}.json"
                    out_path.write_text(
                        json.dumps(timing_data[tf], separators=(",", ":")),
                        encoding="utf-8",
                    )
                    item["hasTiming"] = True
                    timing_count += 1

    base_url = sources.get("d1") or (next(iter(sources.values())) if sources else None)
    info["media"] = {
        "videos": videos,
        "audio": {"base_url": base_url, "items": audio_items},
    }
    info_path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")

    kinds: dict[str, int] = {}
    for v in videos:
        k = v.get("kind") or "other"
        kinds[k] = kinds.get(k, 0) + 1

    return {
        "iso": iso,
        "videos": len(videos),
        "video_kinds": kinds,
        "audio": len(audio_items),
        "timing": timing_count,
        "base_url": base_url,
    }


def main() -> int:
    argv = sys.argv[1:]
    filter_set = set(argv) if argv else None
    concurrency = int(os.environ.get("CONCURRENCY", "6"))

    isos = [
        p.name
        for p in sorted(PKF_ROOT.iterdir())
        if p.is_dir() and not p.name.startswith("_") and (not filter_set or p.name in filter_set)
    ]

    with_video = 0
    with_audio = 0
    with_timing = 0
    done = 0
    video_kind_totals: dict[str, int] = {}

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(process_iso, iso): iso for iso in isos}
        for fut in as_completed(futures):
            iso = futures[fut]
            try:
                r = fut.result()
            except Exception as e:
                print(f"{iso}: {e}", file=sys.stderr)
                continue
            done += 1
            if not r:
                continue
            if r.get("error"):
                print(f"  {iso}: {r['error']}  ({done}/{len(isos)})")
                continue
            if r.get("videos", 0) or r.get("audio", 0):
                if r.get("videos"):
                    with_video += 1
                    for k, v in (r.get("video_kinds") or {}).items():
                        video_kind_totals[k] = video_kind_totals.get(k, 0) + v
                if r.get("audio"):
                    with_audio += 1
                if r.get("timing"):
                    with_timing += 1
                kinds_str = ",".join(f"{k}={v}" for k, v in (r.get("video_kinds") or {}).items())
                timing_str = f"  timing={r.get('timing', 0):>3}" if r.get("timing") else ""
                print(
                    f"  {iso:<6} videos={r['videos']:>3} [{kinds_str}]  "
                    f"audio={r['audio']:>3}{timing_str}  ({done}/{len(isos)})"
                )

    print(
        f"\nDone. {with_video} languages with videos, {with_audio} with audio, "
        f"{with_timing} with timing. Totals by kind: {json.dumps(video_kind_totals)}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
