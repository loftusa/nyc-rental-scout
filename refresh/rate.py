# /// script
# dependencies = ["anthropic>=0.92.0", "pillow>=10.0"]
# ///
"""Rate the shortlisted listings with the Claude API — photos + body text.

For each listing in shortlist.json this script downloads the full photo
gallery, tiles it into one contact-sheet montage (Pillow), and sends batches
of listings (montage image + posting text) to the Messages API with a strict
JSON schema. Writes ratings.json in the exact shape refresh.py --build expects.

Fails loud: exits non-zero unless EVERY listing id gets a valid rating, so a
partial/garbled rating run can never produce a silently-degraded board.

Env:  ANTHROPIC_API_KEY  (required)
      RATE_MODEL         (default claude-sonnet-4-6; claude-haiku-4-5 = cheaper)
Usage: python3 rate.py [--limit N] [--batch-size K]
"""

import argparse
import base64
import io
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import anthropic
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SHORTLIST = os.path.join(HERE, "shortlist.json")
RATINGS = os.path.join(HERE, "ratings.json")

MODEL = os.environ.get("RATE_MODEL", "claude-sonnet-4-6")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# --------------------------------------------------------------------------- #
# EDIT ME — describe who is renting. The more specific this is (budget,
# commute, what "social" means to you, dealbreakers), the better the ratings.
# Keep the price numbers in sync with the CONFIG block in refresh.py.
# --------------------------------------------------------------------------- #
RENTER_BRIEF = """Moving to New York City soon. Apartment/studio budget up to
$4,000/mo (ideally ~$3,000); room in a shared place up to $2,300/mo. Needs an
easy subway commute to Midtown Manhattan, ideally <=30 minutes door to door.
Open to either a solo studio/1BR in a decent area OR a room in a friendly
shared apartment with sociable, compatible housemates. Likes having parks and
some greenery nearby, but this is NYC — realistic expectations."""

RUBRIC = f"""You are rating New York City rental listings for a renter, for a live search board.

WHO IT'S FOR — {RENTER_BRIEF}

Each listing below has a photo MONTAGE (a grid of ALL its photos) and its posting text. LOOK AT EVERY PHOTO in each montage before scoring that listing.

Score each dimension as an integer 1-10, literally as defined:
- nature — proximity to parks/trees/waterfront/greenery (text + what the photos show). 10 = right on a park or waterfront; 1 = bare concrete canyon.
- quiet — residential calm; low traffic/noise. 10 = quiet side street; 1 = above a bar / loud arterial / next to elevated train tracks.
- nice — how desirable/safe/well-kept the area & unit are, per the photos. 10 = clearly nice & good shape; 1 = rough/run-down.
- social — for a SHARED place: housemate/vibe signal — friendly, sociable, compatible households HIGH (7-10); generic shared place with no signal MID (4-6). A solo studio/1BR with no housemates is LOW (1-3) on this dimension, but that must NOT drag the overall read down (the final ranking handles it).
- value — price vs. what you get (space/condition/location per the photos) for New York City. 10 = underpriced for what it is; 1 = overpriced.
- commute — ease of reaching Midtown Manhattan by subway, per the neighborhood stated for the listing. 10 = a short direct ride; 1 = a long multi-transfer trek. (A deterministic model recomputes this downstream; score it honestly anyway.)
- aesthetic — HOW GOOD THE PLACE LOOKS IN ITS PHOTOS, judged ONLY from the montage. Attractive, well-lit, tasteful, clean, good finishes/light/views? 10 = genuinely beautiful, magazine-quality photos of an attractive space. 5 = ordinary/plain but fine. 1-3 = ugly, dark, cluttered, grimy, low-effort/blurry photos, or a clearly unappealing space (or no usable photos). This is weighted into the ranking — do not give high aesthetic to a place whose pictures are bad.
Then:
- fit — overall 1-10 holistic fit for the renter (a deterministic model recomputes the final ranking; give your honest overall anyway). A quiet studio in a nice area can be high fit even with low social.
- why — one line in the renter's terms, referencing what the photos/listing show.
- live — false if the post looks dead/expired/duplicate or like a scam (too cheap for the area, generic copy, off-platform payment, "wire a deposit before viewing").
- commercial — true if it's an office/retail/parking/commercial space, not a home.

NYC-specific cautions: rent-stabilized-sounding deals that are far too cheap are usually scams; watch for photos that are obviously stock/staged renderings rather than the real unit; a "1BR" that is a flex-walled living room should score lower on value.

Rate EVERY listing id you are given, once each."""

SCORE = {"type": "integer"}  # 1-10 validated client-side (API schema rejects min/max)
RATING_SCHEMA = {
    "type": "object",
    "properties": {
        "ratings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "nature": SCORE,
                    "quiet": SCORE,
                    "nice": SCORE,
                    "social": SCORE,
                    "value": SCORE,
                    "commute": SCORE,
                    "aesthetic": SCORE,
                    "fit": SCORE,
                    "why": {"type": "string"},
                    "live": {"type": "boolean"},
                    "commercial": {"type": "boolean"},
                },
                "required": [
                    "id",
                    "nature",
                    "quiet",
                    "nice",
                    "social",
                    "value",
                    "commute",
                    "aesthetic",
                    "fit",
                    "why",
                    "live",
                    "commercial",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ratings"],
    "additionalProperties": False,
}

SCORE_KEYS = (
    "nature",
    "quiet",
    "nice",
    "social",
    "value",
    "commute",
    "aesthetic",
    "fit",
)


def fetch_bytes(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def build_montage(listing, thumb=280, cols=4, max_imgs=24):
    """Tile all of a listing's photos into one JPEG contact sheet. None if no photos."""
    urls = (listing.get("imgs") or [])[:max_imgs]
    thumbs = []
    for u in urls:
        try:
            im = Image.open(io.BytesIO(fetch_bytes(u))).convert("RGB")
            im.thumbnail((thumb, thumb * 3 // 4))
            thumbs.append(im)
        except Exception:
            continue
    if not thumbs:
        return None
    rows = (len(thumbs) + cols - 1) // cols
    cell_h = thumb * 3 // 4
    sheet = Image.new("RGB", (cols * thumb, rows * cell_h), "white")
    for i, im in enumerate(thumbs):
        sheet.paste(im, ((i % cols) * thumb, (i // cols) * cell_h))
    # keep the long edge modest so each montage stays ~2K image tokens
    sheet.thumbnail((1400, 1400))
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=80)
    return base64.standard_b64encode(buf.getvalue()).decode()


def listing_text(r):
    body = (r.get("body") or "")[:1500]
    place = f"{r.get('hood')} ({r.get('region')}) — ~{r.get('min_a', '?')} min transit to anchor"
    return (
        f"### Listing {r['id']} — ${r['price']:,}/mo — {place} — "
        f"{'room in shared home' if r.get('bucket') == 'room' else 'apartment/studio'}\n"
        f"Title: {r.get('title')}\nPosting text: {body or '(no body scraped)'}"
    )


def validate(batch_ids, obj):
    """Return {id: rating} for valid entries; raise ValueError on malformed scores."""
    out = {}
    for rr in obj.get("ratings", []):
        if rr["id"] not in batch_ids:
            continue
        for k in SCORE_KEYS:
            v = rr[k]
            if not isinstance(v, int) or not 1 <= v <= 10:
                raise ValueError(f"{rr['id']}.{k}={v!r} not an int in 1..10")
        out[rr["id"]] = rr
    return out


def rate_batch(client, batch, montages):
    content = [{"type": "text", "text": RUBRIC}]
    for r in batch:
        content.append({"type": "text", "text": listing_text(r)})
        b64 = montages.get(r["id"])
        if b64:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    },
                }
            )
        else:
            content.append(
                {
                    "type": "text",
                    "text": "(no photos could be downloaded for this listing — score aesthetic 2-3)",
                }
            )
    content.append(
        {
            "type": "text",
            "text": f"Now rate ALL {len(batch)} listings above ({', '.join(r['id'] for r in batch)}).",
        }
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        thinking={"type": "disabled"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": RATING_SCHEMA},
        },
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "max_tokens":
        raise RuntimeError("response truncated (max_tokens) — reduce batch size")
    if response.stop_reason == "refusal":
        raise RuntimeError("model refused the batch")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text), response.usage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--limit", type=int, default=0, help="only rate first N (smoke test)"
    )
    ap.add_argument("--batch-size", type=int, default=5)
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "FATAL: ANTHROPIC_API_KEY not set — cannot rate. (Sweep mode needs no key.)"
        )
    sel = json.load(open(SHORTLIST))
    if args.limit:
        sel = sel[: args.limit]
    assert sel, "shortlist.json is empty"
    ids = [r["id"] for r in sel]
    print(f"rating {len(sel)} listings with {MODEL} (batch={args.batch_size})")

    print("building montages...")
    with ThreadPoolExecutor(max_workers=6) as ex:
        montages = dict(ex.map(lambda r: (r["id"], build_montage(r)), sel))
    n_m = sum(1 for v in montages.values() if v)
    print(f"montages: {n_m}/{len(sel)}")

    client = anthropic.Anthropic(max_retries=4)
    ratings, in_tok, out_tok = {}, 0, 0
    batches = [
        sel[i : i + args.batch_size] for i in range(0, len(sel), args.batch_size)
    ]
    for bi, batch in enumerate(batches):
        batch_ids = {r["id"] for r in batch}
        got = {}
        for attempt in (1, 2):
            todo = [r for r in batch if r["id"] not in got]
            if not todo:
                break
            try:
                obj, usage = rate_batch(client, todo, montages)
                got.update(validate({r["id"] for r in todo}, obj))
                in_tok += usage.input_tokens
                out_tok += usage.output_tokens
            except (json.JSONDecodeError, ValueError, RuntimeError, KeyError) as e:
                print(f"  batch {bi+1} attempt {attempt} problem: {e}", file=sys.stderr)
        missing = batch_ids - set(got)
        if missing:
            sys.exit(
                f"FATAL: could not get valid ratings for {sorted(missing)} after retries."
            )
        ratings.update(got)
        print(f"  batch {bi+1}/{len(batches)}: {len(got)}/{len(batch)} rated")

    assert set(ids) == set(ratings), "coverage check failed"
    json.dump([ratings[i] for i in ids], open(RATINGS, "w"), indent=1)
    flagged = [i for i in ids if not ratings[i]["live"] or ratings[i]["commercial"]]
    print(
        f"wrote {RATINGS}: {len(ratings)} ratings | dead/commercial flagged: {flagged or 'none'}\n"
        f"tokens: {in_tok:,} in / {out_tok:,} out"
    )


if __name__ == "__main__":
    main()
