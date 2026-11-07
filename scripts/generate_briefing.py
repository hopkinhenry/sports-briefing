#!/usr/bin/env python3
"""Generate data/briefing.json using Gemini 2.5 Flash with Google Search grounding.

Usage:
    GEMINI_API_KEY=... python scripts/generate_briefing.py            # always generate
    GEMINI_API_KEY=... python scripts/generate_briefing.py --scheduled  # only if it's >= 4 AM ET
                                                                          # and today's briefing doesn't exist yet
Optional:
    YOUTUBE_API_KEY=...   enables embedded highlight videos (see README)
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent))
import media  # noqa: E402

MODEL = "gemini-2.5-flash"
TZ = ZoneInfo("America/New_York")
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "briefing.json"
MAX_ATTEMPTS = 4

LEAGUES = ["NBA", "NFL", "College Football", "Formula 1"]

PRIORITY_TEAMS = [
    "Los Angeles Lakers (NBA)",
    "USC Trojans (college football)",
    "Notre Dame Fighting Irish (college football)",
    "Caleb Williams / Chicago Bears (NFL)",
    "Sam Darnold / Seattle Seahawks (NFL)",
]

JSON_SHAPE = """{
  "watercooler": {
    "headline": "The single biggest sports story people will be talking about today (max ~12 words)",
    "summary": "3-5 sentences: what happened, who was involved, the key numbers/score",
    "why_it_matters": "1-2 sentences on the bigger implication (standings, playoffs, legacy, drama)",
    "video_query": "Short YouTube search for official highlights or the press conference for this story, e.g. 'Lakers vs Suns highlights'",
    "reactions": [
      {"person": "Full name", "role": "e.g. 'ESPN NFL insider' or 'Lakers forward'", "platform": "Where they said it: X, Instagram, TV, press conference, podcast", "text": "What they said (under 25 words)", "is_direct_quote": true, "source_url": "Exact URL of the search-result article that reports it"}
    ]
  },
  "priority_teams": [
    {
      "name": "Team or player name",
      "status": "Record / standing / ranking, e.g. '5-3, 2nd in West' or '#7 in CFP rankings'",
      "last_result": "Most recent game result with score and 1 standout detail (or 'No game' / 'Bye week' / 'Offseason')",
      "storyline": "2-3 sentences: the trend, injuries, performance, or narrative that matters right now",
      "next_up": "Next opponent and date/time ET, if known",
      "video_query": "e.g. 'Caleb Williams Bears highlights'"
    }
  ],
  "around_the_leagues": {
    "NBA": [{"headline": "...", "detail": "1-2 sentences", "video_query": "...", "reactions": []}],
    "NFL": [{"headline": "...", "detail": "1-2 sentences", "video_query": "...", "reactions": []}],
    "College Football": [{"headline": "...", "detail": "1-2 sentences", "video_query": "...", "reactions": []}],
    "Formula 1": [{"headline": "...", "detail": "1-2 sentences", "video_query": "...", "reactions": []}]
  },
  "talking_points": [
    {"opener": "A natural, conversational line to start a watercooler chat", "follow_up": "A fun fact, stat, or hot take to keep it going"},
    {"opener": "...", "follow_up": "..."}
  ]
}"""


def build_prompt(now: datetime) -> str:
    today = now.strftime("%A, %B %d, %Y")
    last_night = (now - timedelta(days=1)).strftime("%A, %B %d, %Y")
    teams = "\n".join(f"  - {t}" for t in PRIORITY_TEAMS)
    leagues = ", ".join(LEAGUES)
    return f"""You are the editor of a personal morning sports briefing. Today is {today} (US Eastern time).
Use Google Search to get CURRENT, accurate information. Do not rely on memory for scores, standings, rosters, or schedules.

COVERAGE
Leagues: {leagues}.
Look at games and sessions from last night ({last_night}) and the last 24-48 hours, plus broader storylines
(standings, injuries, trades, rankings, playoff/championship races, controversies, trends).

PRIORITY TEAMS / PLAYERS (always include one entry for each, in this order):
{teams}

Verify each player's CURRENT team via search. If a player has changed teams, report the real current team.
If a league or team is in its offseason or on a bye, say so briefly and give the most relevant news instead.

SECTIONS
1. watercooler: the one story an average daily sports fan would most want to talk about today.
   Pick across all sports, not only the leagues above if something enormous happened, but prefer them.
2. priority_teams: exactly one entry per priority team/player listed above.
3. around_the_leagues: 2-4 items for each of NBA, NFL, College Football, Formula 1.
   Cover results, trends, standings movement, and storylines. Skip a league's results only if nothing is happening, and say why.
4. talking_points: exactly 2 conversational talking points a person could use casually at work.

REACTIONS
- For the watercooler story, find 2-4 reactions from well-respected people in sports: current or former players,
  coaches, executives, and established reporters or analysts. For the top story in a league, 0-2 more if notable.
- ONLY include a reaction you actually found in the search results. Never invent, guess, or reconstruct a quote or a post.
  If you cannot find any, return an empty array. Fewer real reactions beat more doubtful ones.
- "source_url" must be the exact URL of a news article or page from your search results that reports the reaction.
  Do not use a homepage, and do not link x.com, instagram.com or similar directly; cite the article that reports the post.
- Keep each reaction under 25 words. Set is_direct_quote to true only if the words are verbatim; otherwise paraphrase
  and set it to false. "platform" is where the person said it.

VIDEOS
- For every video_query, write a short, specific YouTube search that would find official highlights or a press conference
  for that exact story (team names, opponent, player). No dates, no quotes.

STYLE
- Concise, confident, engaging; plain text only (no markdown, no citation markers like [1]).
- Include real scores and stats. Never invent facts. If unsure, leave it out.

OUTPUT FORMAT
Return ONLY a single valid JSON object matching this shape exactly (no code fences, no commentary):
{JSON_SHAPE}
"""


def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model output")
    return json.loads(text[start : end + 1])


def validate(data: dict) -> None:
    for key in ("watercooler", "priority_teams", "around_the_leagues", "talking_points"):
        if key not in data:
            raise ValueError(f"Missing section: {key}")
    for k in ("headline", "summary", "why_it_matters"):
        if not data["watercooler"].get(k):
            raise ValueError(f"watercooler.{k} missing")
    if not isinstance(data["priority_teams"], list) or len(data["priority_teams"]) < len(PRIORITY_TEAMS):
        raise ValueError("priority_teams incomplete")
    for league in LEAGUES:
        if not data["around_the_leagues"].get(league):
            raise ValueError(f"around_the_leagues.{league} missing or empty")
    if not isinstance(data["talking_points"], list) or len(data["talking_points"]) != 2:
        raise ValueError("talking_points must contain exactly 2 items")


def extract_sources(response) -> list:
    sources, seen = [], set()
    try:
        meta = response.candidates[0].grounding_metadata
        for chunk in (meta.grounding_chunks or []):
            web = getattr(chunk, "web", None)
            if web and web.uri and web.uri not in seen:
                seen.add(web.uri)
                sources.append({"title": web.title or web.uri, "url": web.uri})
    except (AttributeError, IndexError, TypeError):
        pass
    return sources[:12]


def generate(now: datetime):
    """Returns (data, grounding). Retries only the Gemini call, never the media step."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY is not set")
    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=0.4,
    )
    # NOTE: Gemini can't combine Google Search grounding with response_mime_type="application/json",
    # so we ask for JSON in the prompt and parse/validate it ourselves (with retries).
    prompt = build_prompt(now)
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(model=MODEL, contents=prompt, config=config)
            data = extract_json(response.text or "")
            validate(data)
            data["sources"] = extract_sources(response)
            return data, media.grounding_from_response(response)
        except Exception as e:  # noqa: BLE001 - retry on API errors, rate limits, and bad JSON alike
            last_err = e
            wait = 15 * attempt
            print(f"Attempt {attempt}/{MAX_ATTEMPTS} failed: {e}. Retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)
    sys.exit(f"Failed to generate briefing: {last_err}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduled", action="store_true",
                        help="Skip unless it's >= 4 AM ET and today's briefing doesn't exist yet.")
    args = parser.parse_args()

    now = datetime.now(TZ)
    today_iso = now.strftime("%Y-%m-%d")

    if args.scheduled:
        if now.hour < 4:
            print(f"It's {now:%H:%M} ET (before 4 AM) - skipping (DST double-cron).")
            return
        if OUT_PATH.exists():
            try:
                if json.loads(OUT_PATH.read_text()).get("date") == today_iso:
                    print("Today's briefing already exists - skipping.")
                    return
            except json.JSONDecodeError:
                pass

    data, grounding = generate(now)

    # Photos, videos and verified reactions are best-effort extras: never let them sink the briefing.
    try:
        stats = media.enrich(data, grounding, now, youtube_key=os.environ.get("YOUTUBE_API_KEY"))
        print(f"Media: {stats}")
    except Exception as e:  # noqa: BLE001
        print(f"Media step failed ({type(e).__name__}: {e}) - publishing text-only briefing.", file=sys.stderr)
        leagues_items = [i for v in data.get("around_the_leagues", {}).values() for i in v]
        for item in [data.get("watercooler", {})] + data.get("priority_teams", []) + leagues_items:
            if isinstance(item, dict):
                item.pop("reactions", None)  # never ship unverified reactions
                item.pop("video_query", None)

    result = {
        "date": today_iso,
        "date_label": now.strftime("%A, %B %-d, %Y"),
        "generated_at": now.isoformat(timespec="seconds"),
        "model": MODEL,
        **data,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
