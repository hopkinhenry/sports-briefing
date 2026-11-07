# Morning Sports Briefing

A free, personalized daily sports briefing. A GitHub Action asks Gemini 2.5 Flash (with Google Search grounding) for your briefing every morning at 4:00 AM Eastern, saves it as `data/briefing.json`, and a static mobile-first page displays it.

Covers NBA, NFL, College Football and Formula 1, with a priority check-in on the Lakers, USC, Notre Dame, Caleb Williams / Bears and Sam Darnold / Seahawks.

## Setup (about 10 minutes)

1. **Get a free Gemini API key** at https://aistudio.google.com/apikey
2. **Create a GitHub repo** and push this folder to it.
3. **Add the secret:** repo → Settings → Secrets and variables → Actions → New repository secret
   - Name: `GEMINI_API_KEY`, Value: your key
4. **Allow the bot to commit:** Settings → Actions → General → Workflow permissions → *Read and write permissions*
5. **Run it once:** Actions tab → *Daily Sports Briefing* → *Run workflow*. This replaces the sample data with a real briefing.
6. **Host the page (free), pick one:**
   - GitHub Pages: Settings → Pages → Deploy from branch → `main` / root. Your URL will be `https://<you>.github.io/<repo>/`
   - Vercel: import the repo, framework preset "Other", no build command. It redeploys on every commit.
7. **Add to your phone's home screen:** open the URL in Safari → Share → Add to Home Screen (Android Chrome: menu → Install app).

## Schedule

GitHub cron runs in UTC only, so the workflow has two triggers (08:00 and 09:00 UTC). One of them is 4 AM Eastern depending on daylight saving. The script's `--scheduled` flag skips the early run and skips if today's briefing already exists, so you get exactly one briefing per day. GitHub scheduled runs can start a few minutes late during busy periods.

## Run locally

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=your_key
python scripts/generate_briefing.py
python -m http.server 8000   # then open http://localhost:8000
```

## Customizing

Edit `PRIORITY_TEAMS` and `LEAGUES` at the top of `scripts/generate_briefing.py`. If you add a league, add its key to the JSON shape in the same file and it will render automatically.

## Notes

- Google Search grounding on the Gemini free tier has a daily request cap; one run per day is well within it. Free-tier limits and model availability can change, so check Google AI Studio if runs start failing.
- Gemini can't combine search grounding with forced JSON output, so the script asks for JSON in the prompt, then parses, validates and retries up to 4 times.
- Output is AI-generated from search results. Scores and stats are usually right but worth a double-check before you quote them.
- The workflow fails loudly if all retries fail, so you'll get GitHub's failure email. The page also warns when the briefing is 2+ days old.
