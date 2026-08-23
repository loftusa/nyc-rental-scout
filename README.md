# NYC rental scout

A self-refreshing apartment-hunt board for New York City: it pulls real,
currently-listed rentals from Craigslist (plus Rent.com managed complexes),
scrapes every listing's full photo gallery and posting text, has Claude look
at all the photos and score each place on nature / quiet / niceness / social
scene / value / photo quality, and renders everything as an interactive map +
ranked board. You drag sliders for what matters to you and the whole board
re-ranks live.

Forked from [alex-loftus.com/houses](https://alex-loftus.com/houses) (Bay
Area version) with all personal data stripped and the config moved to the
top of the pipeline files.

```
refresh/refresh.py --pull   Craigslist sapi API + Rent.com  ->  refresh/shortlist.json
refresh/rate.py             Claude rates photos + text      ->  refresh/ratings.json
refresh/refresh.py --build  merge + rank                    ->  data.js
index.html + fitmath.js     static viewer (Leaflet map, sliders, cards)
refresh/refresh.py --sweep  keyless: prune dead listings from data.js
```

## Quick start

1. **Configure.** Open `refresh/refresh.py` and edit the `CONFIG` block at
   the top: your commute anchor(s), budget, and optionally the neighborhoods
   you love. Then open `refresh/rate.py` and edit `RENTER_BRIEF` (who you
   are, what you want — this steers the LLM ratings). Keep the two files'
   budget numbers in sync.

2. **Pull + rate + build:**

   ```bash
   python3 refresh/refresh.py --pull        # no key needed; ~2-4 min
   export ANTHROPIC_API_KEY=sk-ant-...      # console.anthropic.com
   python3 refresh/rate.py                  # ~$0.30 with the default model
   python3 refresh/refresh.py --build       # writes data.js
   ```

   `rate.py` needs `anthropic` and `pillow` (`pip install anthropic pillow`,
   or run it with `uv run refresh/rate.py` — it has inline deps).

3. **View it:**

   ```bash
   python3 -m http.server 8000
   # open http://localhost:8000
   ```

   (Opening `index.html` directly with `file://` also works in most
   browsers.)

## Keeping it fresh

- `--sweep` re-checks every shown listing and prunes ones whose posting was
  deleted/expired. Free, no API key — run it a few times a day.
- A full pull + rate + build once a day keeps the board current.

### GitHub Actions (recommended)

Push this folder to a GitHub repo and the included workflow
(`.github/workflows/refresh.yml`) does the daily full refresh + 6-hourly
sweeps for you and commits `data.js`. Craigslist is reachable from Actions
runners even when it blocks datacenter IPs elsewhere. Setup:

1. Create a repo, push, and add an `ANTHROPIC_API_KEY` repository secret
   (Settings → Secrets → Actions). Without the secret, daily runs degrade to
   a sweep instead of failing.
2. Enable GitHub Pages (Settings → Pages → deploy from branch, root) and the
   board is live at `https://<you>.github.io/<repo>/`.

## Tuning

| What | Where |
|---|---|
| Commute anchor(s) | `ANCHOR_A` / `ANCHOR_B` in `refresh/refresh.py` (label, short label, lat, lon). One anchor is normal; set a second to reward places reachable to both. |
| Budget | `APT_MIN/MAX`, `ROOM_MIN/MAX`, `*_TARGET` in `refresh/refresh.py` — and mirror them in `RENTER_BRIEF` in `rate.py`. |
| Search zone | `CENTERS` (Craigslist geo searches) and `MAX_MILES_FROM_ANCHOR`. |
| Default slider weights | `WEIGHTS` in `refresh/refresh.py`. |
| Neighborhood priors | the `RULES` table — seed estimates (region, subway minutes to Midtown, nature/quiet/nice/social 0-5). They only seed the shortlist; the LLM rating of the actual listing does the heavy lifting. |
| Rating model | `RATE_MODEL` env var (default `claude-sonnet-4-6`; `claude-haiku-4-5` is cheaper, `claude-opus-5` sharper). |
| Who you are | `RENTER_BRIEF` in `refresh/rate.py`. |

## Notes & limitations

- Craigslist is the backbone. Rent.com is best-effort (its page layout
  changes sometimes; failures only warn). StreetEasy/Zillow block scraping —
  the "Browse it yourself" section links pre-filtered live searches instead.
- Commute minutes are estimates: hand-tuned subway priors for known
  neighborhoods, a distance model otherwise. Treat them as a prior, check
  Google Maps before you fall in love with a place.
- "Reached out" and "hidden" marks are saved in your browser's localStorage
  only (the original had a backend for cross-device sync; this fork doesn't).
- The scam warning in the footer is serious: never wire money or pay a
  deposit before seeing a unit in person.
