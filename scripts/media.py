"""Media enrichment for the briefing: article photos, YouTube videos, verified reactions.

Everything here is best-effort. A failure in any step just means that item ends up without
media; it never fails the briefing.

- Photos:    the og:image of the news article that Gemini's search grounding used for that story
             (the same thing a link preview shows). Hotlinked with credit + link back, never stored.
- Videos:    YouTube Data API search, restricted to an allowlist of official channels, shown with
             YouTube's own embed player. Needs YOUTUBE_API_KEY; skipped if it isn't set.
- Reactions: Gemini proposes them, then each one is checked against the page it cites.
             Direct quotes must appear in that page; paraphrases must at least match its content.
             Anything unverifiable is dropped.
"""
import html as htmllib
import ipaddress
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from html.parser import HTMLParser

UA = "Mozilla/5.0 (compatible; MorningSportsBriefing/1.0; personal use)"
TIMEOUT = 8
MAX_BYTES = 700_000
VIDEO_LOOKBACK_DAYS = 4
MAX_WATERCOOLER_VIDEOS = 2
MAX_WATERCOOLER_REACTIONS = 4
MAX_ITEM_REACTIONS = 2
MAX_DIRECT_QUOTE_WORDS = 30

# Only videos from these channels are shown. Edit to taste.
# Exact matches are used for short names that would otherwise match fan/meme channels.
ALLOWED_CHANNELS_EXACT = {
    "nba", "nfl", "espn", "f1", "formula 1", "bleacher report", "house of highlights",
    "cbs sports", "cbs sports hq", "fox sports", "nbc sports", "lakers", "los angeles lakers",
    "usc football", "usc trojans", "notre dame football", "notre dame", "chicago bears",
    "seattle seahawks", "big ten network", "sky sports", "sky sports f1", "sec network",
    "espn college football", "the pat mcafee show", "pat mcafee",
}
ALLOWED_CHANNEL_PREFIXES = (
    "espn ", "fox sports", "nbc sports", "cbs sports", "sky sports", "formula 1",
    "bleacher report", "big ten", "usc ", "notre dame ", "chicago bears", "seattle seahawks",
)


def _log(msg):
    print(f"[media] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- text helpers
def _norm(s):
    s = re.sub(r"[^a-z0-9\s]", " ", str(s).lower())
    return re.sub(r"\s+", " ", s).strip()


def _shingles(words, n):
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def _overlap(words, target_set, n):
    sh = [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]
    if not sh:
        return 0.0
    return sum(1 for s in sh if s in target_set) / len(sh)


# --------------------------------------------------------------------------- page fetching
class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.title, self._in_title = {}, "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            val = a.get("content")
            if key and val and key not in self.meta:
                self.meta[key] = val
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _visible_text(raw_html):
    t = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw_html)
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return htmllib.unescape(t)


def _is_public_host(host):
    try:
        infos = socket.getaddrinfo(host, None)
        return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)
    except Exception:  # noqa: BLE001
        return False


_page_cache = {}


def fetch_page(url):
    """Fetch an article page. Returns {url, site, title, image, text} or None."""
    if not url:
        return None
    if url in _page_cache:
        return _page_cache[url]
    result = None
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme in ("http", "https") and p.hostname and _is_public_host(p.hostname):
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                final = r.geturl()
                ctype = (r.headers.get("Content-Type") or "").lower()
                fp = urllib.parse.urlparse(final)
                if ("html" in ctype or not ctype) and fp.hostname and _is_public_host(fp.hostname):
                    raw = r.read(MAX_BYTES).decode(r.headers.get_content_charset() or "utf-8", errors="replace")
                    parser = _MetaParser()
                    try:
                        parser.feed(raw)
                    except Exception:  # noqa: BLE001
                        pass
                    m = parser.meta
                    img = m.get("og:image:secure_url") or m.get("og:image") or m.get("twitter:image") or ""
                    if img:
                        img = urllib.parse.urljoin(final, img)
                    domain = (fp.hostname or "").removeprefix("www.")
                    result = {
                        "url": final,
                        "site": m.get("og:site_name") or domain,
                        "title": htmllib.unescape((m.get("og:title") or parser.title or "")).strip(),
                        "image": img,
                        "text": _visible_text(raw),
                    }
    except Exception as e:  # noqa: BLE001
        _log(f"fetch failed ({type(e).__name__}): {urllib.parse.urlparse(url).hostname}")
    _page_cache[url] = result
    return result


def image_ok(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            ct = (r.headers.get("Content-Type") or "").lower()
            return r.status < 400 and (not ct or ct.startswith("image/"))
    except Exception:  # noqa: BLE001
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                ct = (r.headers.get("Content-Type") or "").lower()
                return r.status < 400 and (not ct or ct.startswith("image/"))
        except Exception:  # noqa: BLE001
            return False


def _usable_image(page):
    if not page:
        return False
    img = page.get("image") or ""
    if not img.startswith("https://"):
        return False
    low = img.lower()
    if any(w in low for w in ("logo", "favicon", "placeholder", "default-", "/default")):
        return False
    if urllib.parse.urlparse(page["url"]).path in ("", "/"):
        return False  # homepages/section pages usually share a generic brand image
    return True


# --------------------------------------------------------------------------- grounding
def grounding_from_response(response):
    """Pull chunks (sources) and supports (which text each source backs) out of the SDK response."""
    chunks, supports = [], []
    try:
        meta = response.candidates[0].grounding_metadata
        for ch in (meta.grounding_chunks or []):
            web = getattr(ch, "web", None)
            chunks.append({"uri": (web.uri if web and web.uri else ""), "title": ((web.title if web else "") or "")})
        for s in (meta.grounding_supports or []):
            seg = getattr(s, "segment", None)
            supports.append({
                "text": getattr(seg, "text", "") or "",
                "indices": list(getattr(s, "grounding_chunk_indices", None) or []),
            })
    except (AttributeError, IndexError, TypeError):
        pass
    return {"chunks": chunks, "supports": supports}


def _item_text_fields(item):
    skip = {"video_query"}
    return [v for k, v in item.items() if isinstance(v, str) and k not in skip and v.strip()]


def _chunk_indices_for(item, supports):
    """Which source chunks back this item's text? Matches support segments to the item by word-shingle overlap."""
    blob_words = _norm(" ".join(_item_text_fields(item))).split()
    blob_str = " ".join(blob_words)
    blob_set = _shingles(blob_words, 5)
    found = []
    for s in supports:
        seg = _norm(s.get("text", ""))
        words = seg.split()
        if len(seg) < 25:
            continue
        if len(words) < 5:
            hit = seg in blob_str
        else:
            hit = _overlap(words, blob_set, 5) >= 0.4
        if hit:
            for i in s.get("indices", []):
                if i not in found:
                    found.append(i)
    return found


def _build_sources(chunks, pages):
    out, seen = [], set()
    for c, pg in zip(chunks, pages):
        url = pg["url"] if pg else c.get("uri", "")
        if not url or url in seen:
            continue
        seen.add(url)
        title = (pg["title"] if pg and pg.get("title") else "") or c.get("title") or url
        out.append({"title": title, "url": url})
    return out[:12]


# --------------------------------------------------------------------------- photos
def _assign_image(item, indices, pages, used):
    for i in indices:
        if not (0 <= i < len(pages)):
            continue
        pg = pages[i]
        if not _usable_image(pg) or pg["image"] in used:
            continue
        if not image_ok(pg["image"]):
            continue
        used.add(pg["image"])
        item["image"] = {"url": pg["image"], "credit": pg["site"], "page_url": pg["url"], "title": pg["title"]}
        return True
    return False


# --------------------------------------------------------------------------- videos
def _channel_allowed(name):
    n = _norm(name)
    return n in ALLOWED_CHANNELS_EXACT or n.startswith(ALLOWED_CHANNEL_PREFIXES)


class _QuotaStop(Exception):
    pass


def youtube_search(query, api_key, published_after, limit=1, skip_ids=()):
    params = {
        "part": "snippet", "q": query, "type": "video", "videoEmbeddable": "true",
        "videoSyndicated": "true", "maxResults": 15, "publishedAfter": published_after,
        "regionCode": "US", "relevanceLanguage": "en", "key": api_key,
    }
    url = "https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=TIMEOUT) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        _log(f"YouTube API HTTP {e.code} (check the key / that YouTube Data API v3 is enabled / daily quota)")
        if e.code in (400, 403):
            raise _QuotaStop() from None
        return []
    except Exception as e:  # noqa: BLE001
        _log(f"YouTube search failed ({type(e).__name__})")
        return []
    out = []
    for it in payload.get("items", []):
        vid = (it.get("id") or {}).get("videoId")
        sn = it.get("snippet") or {}
        if not vid or vid in skip_ids or not _channel_allowed(sn.get("channelTitle", "")):
            continue
        out.append({
            "id": vid,
            "title": htmllib.unescape(sn.get("title", "")),
            "channel": sn.get("channelTitle", ""),
            "published": sn.get("publishedAt", ""),
        })
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- reactions
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def verify_reaction(r):
    """Return a cleaned reaction if the cited page backs it up, else None."""
    if not isinstance(r, dict):
        return None
    person, text, url = (str(r.get(k, "")).strip() for k in ("person", "text", "source_url"))
    if not (person and text and url):
        return None
    pg = fetch_page(url)
    if not pg:
        return None
    page_norm = _norm(pg["text"])
    page_words = page_norm.split()
    name_words = [w for w in _norm(person).split() if w not in _SUFFIXES]
    if not name_words or f" {name_words[-1]} " not in f" {page_norm} ":
        return None

    qn = _norm(text)
    qwords = qn.split()
    direct = bool(r.get("is_direct_quote"))
    if direct:
        if len(qwords) < 3 or len(qwords) > MAX_DIRECT_QUOTE_WORDS:
            return None
        if qn not in page_norm and _overlap(qwords, _shingles(page_words, 3), 3) < 0.8:
            return None
    else:
        distinct = {w for w in qwords if len(w) > 4}
        if distinct and len(distinct & set(page_words)) / len(distinct) < 0.5:
            return None
    return {
        "person": person,
        "role": str(r.get("role", "")).strip()[:120],
        "platform": str(r.get("platform", "")).strip()[:40],
        "text": text[:300],
        "is_direct_quote": direct,
        "source_url": pg["url"],
        "source_name": pg["site"],
    }


def _verify_list(raw, limit):
    if not isinstance(raw, list):
        return []
    good, seen = [], set()
    for r in raw:
        v = verify_reaction(r)
        if v and (v["person"], v["text"]) not in seen:
            seen.add((v["person"], v["text"]))
            good.append(v)
        if len(good) >= limit:
            break
    return good


# --------------------------------------------------------------------------- orchestration
def enrich(data, grounding, now, youtube_key=None):
    """Add image / videos / verified reactions to data in place. Returns a stats dict."""
    stats = {"images": 0, "videos": 0, "reactions": 0, "reactions_dropped": 0}
    chunks = grounding.get("chunks", [])
    supports = grounding.get("supports", [])

    with ThreadPoolExecutor(max_workers=6) as ex:
        pages = list(ex.map(lambda c: fetch_page(c.get("uri", "")), chunks))
    data["sources"] = _build_sources(chunks, pages)

    wc = data.get("watercooler") or {}
    teams = [t for t in data.get("priority_teams", []) if isinstance(t, dict)]
    leagues = data.get("around_the_leagues") or {}

    # --- photos (one per watercooler story, per priority team, and per league)
    used = set()
    try:
        if _assign_image(wc, _chunk_indices_for(wc, supports), pages, used):
            stats["images"] += 1
        for t in teams:
            if _assign_image(t, _chunk_indices_for(t, supports), pages, used):
                stats["images"] += 1
        for items in leagues.values():
            for it in (items or []):
                if isinstance(it, dict) and _assign_image(it, _chunk_indices_for(it, supports), pages, used):
                    stats["images"] += 1
                    break
    except Exception as e:  # noqa: BLE001
        _log(f"photo step failed: {type(e).__name__}: {e}")

    # --- videos
    if youtube_key:
        after = (now - timedelta(days=VIDEO_LOOKBACK_DAYS)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        seen_ids = set()
        jobs = [(wc, wc.get("video_query") or wc.get("headline"), MAX_WATERCOOLER_VIDEOS)]
        jobs += [(t, t.get("video_query") or f'{t.get("name", "")} highlights', 1) for t in teams]
        for items in leagues.values():
            first = next((i for i in (items or []) if isinstance(i, dict)), None)
            if first:
                jobs.append((first, first.get("video_query") or first.get("headline"), 1))
        try:
            for item, query, n in jobs:
                if not query:
                    continue
                vids = youtube_search(str(query), youtube_key, after, limit=n, skip_ids=seen_ids)
                if vids:
                    item["videos"] = vids
                    seen_ids.update(v["id"] for v in vids)
                    stats["videos"] += len(vids)
        except _QuotaStop:
            _log("stopping video lookups for this run")
        except Exception as e:  # noqa: BLE001
            _log(f"video step failed: {type(e).__name__}: {e}")
    else:
        _log("YOUTUBE_API_KEY not set - skipping videos")

    # --- reactions (verify, then keep only what checks out)
    try:
        targets = [(wc, MAX_WATERCOOLER_REACTIONS)]
        for items in leagues.values():
            targets += [(i, MAX_ITEM_REACTIONS) for i in (items or []) if isinstance(i, dict)]
        for item, limit in targets:
            raw = item.get("reactions")
            good = _verify_list(raw, limit)
            stats["reactions"] += len(good)
            stats["reactions_dropped"] += (len(raw) if isinstance(raw, list) else 0) - len(good)
            if good:
                item["reactions"] = good
            else:
                item.pop("reactions", None)
    except Exception as e:  # noqa: BLE001
        _log(f"reaction step failed: {type(e).__name__}: {e}")
        for item in [wc] + [i for items in leagues.values() for i in (items or []) if isinstance(i, dict)]:
            item.pop("reactions", None)  # never ship unverified reactions

    # --- tidy
    for item in [wc] + teams + [i for items in leagues.values() for i in (items or []) if isinstance(i, dict)]:
        item.pop("video_query", None)
    return stats
