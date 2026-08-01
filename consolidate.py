"""Stage 9 — consolidate several hand-made playlists into a new one.

Some playlists go in whole (`--keep-whole`); others are genre-filtered first
(`--filter`) so their hip-hop and rap does not come along. The result is a NEW
playlist. No source is modified. Nothing is ever deleted or unfollowed: Spotify
has no delete-playlist API at all, and the `Spotify` client this imports
deliberately implements no delete verb.

The genre judgement is a weighted balance between two tag families rather than a
veto list or an allow list, because both simpler rules fail on this library's own
data. A veto ("drop anything tagged rap") deletes Daft Punk, who carries one rap
tag against ninety-two electronic ones. An allow list ("keep anything tagged
electronic") keeps Kendrick Lamar, who carries one electronic tag against
fifty-eight rap ones. Weighting puts them at 0.01 and 0.97.

Dry run by default. Stage 8 writes by default and that is right for Stage 8 — it
only ever touches playlists it created. This stage reads playlists a person built,
so it prints what it would do and waits to be asked twice.

    .venv/bin/python consolidate.py --keep-whole "New" --filter "Old"
    .venv/bin/python consolidate.py --keep-whole "New" --keep-whole "Other" \\
        --filter "Old" --write

Names are matched EXACTLY, case and trailing spaces included — a near-miss is
somebody's other playlist, and reading the wrong source silently consolidates
the wrong music.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter

import duckdb

import config
from enrich import normalise
from playlists import SP_API, Spotify, _title_key
from poll import access_token, load_tokens, missing_scopes

# Read is enough for a dry run; the write path asks for the modify scope too.
SCOPES_READ = "playlist-read-private playlist-read-collaborative"
SCOPES_WRITE = SCOPES_READ + " playlist-modify-private"

KEEP, DROP, REVIEW = "keep", "drop", "review"

REVIEW_COLS = ["decision", "source", "artist_name", "track_name", "rap_share",
               "reason", "added_at", "tags", "spotify_track_uri"]

# Stage 9's own resolution cache, deliberately NOT enrich.py's.
#
# Reading enrich's cache is free and correct — an artist it already resolved
# should never cost a second request. Writing to it is not: enrich.write_outputs
# flattens that whole cache into artist_tags.parquet, so pushing playlist-only
# artists in would seed a listening-history artifact with acts carrying no
# listening time at all, and shift Stage 2's own coverage figures. Different
# population, different file. Append-only and fsynced per record like every
# other cache here, so a quarterly re-run spends nothing it has already spent.
CONSOLIDATE_CACHE = config.CACHE_DIR / "consolidate_artists.jsonl"


# --------------------------------------------------------------------------
# Tag families — precedence, then a blocklist. Neither is optional.
# --------------------------------------------------------------------------


def tag_family(tag: str) -> str | None:
    """Which family a MusicBrainz genre tag belongs to, or None for off-family.

    Rap is tested FIRST and wins outright. This library's vocabulary genuinely
    overlaps: `hardcore hip hop` carries 48 artists and matches both pattern
    lists, and it is rap, not hardcore techno. Testing electronic first would
    quietly rescue every one of them.

    The blocklist then drops tags that match an electronic pattern while being
    nothing of the kind — `garage rock` is not UK garage, `hardcore punk` is not
    gabber. Anything matching neither list is off-family and goes to review
    rather than to a guess.
    """
    t = (tag or "").strip().lower()
    if not t:
        return None
    if any(p in t for p in config.CONSOLIDATE_RAP_PATTERNS):
        return "rap"
    if any(b in t for b in config.CONSOLIDATE_EDM_BLOCKLIST):
        return None
    if any(p in t for p in config.CONSOLIDATE_EDM_PATTERNS):
        return "edm"
    return None


def artist_family_weights(con: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    """Per-artist rap/electronic weight from the Stage 2 tag table.

    Votes are clamped at 0 the way Stage 3 clamps them — MusicBrainz counts go
    negative on downvotes — and then, if BOTH families come out at zero weight
    for an artist who does carry family tags, the score falls back to counting
    tags instead of votes. Without that fallback an artist whose tags all sit at
    zero votes scores 0/0 and is called off-family despite being tagged. This is
    the same failure MIN_TAG_COUNT_FOR_ANCHOR exists to describe: REAPER carries
    `heavy metal` at zero votes and it is still a real tag.
    """
    rows = con.sql("""
        SELECT artist_name, tag, GREATEST(COALESCE(tag_count, 0), 0) AS w
        FROM artist_tags
        WHERE is_genre AND artist_name IS NOT NULL
    """).fetchall()

    by_artist: dict[str, list] = {}
    for artist, tag, w in rows:
        by_artist.setdefault(artist, []).append({"tag": tag, "count": w})
    # One scoring path shared with resolve_missing(): the zero-vote fallback is
    # an invariant, and two copies of it would eventually disagree.
    return {normalise(a): _weights_from_tags(a, tags)
            for a, tags in by_artist.items()}


def _weights_from_tags(name: str, tags: list[dict]) -> dict:
    """Build one weights record from raw MusicBrainz tag dicts."""
    rec = {"artist_name": name, "rap_w": 0.0, "edm_w": 0.0,
           "rap_n": 0, "edm_n": 0, "tags": [], "n_tags": len(tags)}
    for t in tags:
        fam = tag_family(t.get("tag", ""))
        if not fam:
            continue
        w = max(int(t.get("count") or 0), 0)
        rec[f"{fam}_w"] += float(w)
        rec[f"{fam}_n"] += 1
        rec["tags"].append(f"{t['tag']}({w})")
    rap_w, edm_w = rec["rap_w"], rec["edm_w"]
    if rap_w == 0 and edm_w == 0 and (rec["rap_n"] or rec["edm_n"]):
        rap_w, edm_w = float(rec["rap_n"]), float(rec["edm_n"])
    total = rap_w + edm_w
    rec["rap_share"] = (rap_w / total) if total else None
    return rec


def resolve_missing(names: list[str], weights: dict) -> int:
    """Ask MusicBrainz about artists Stage 2 has never covered.

    These are real: a playlist carries acts the listening history does not, and
    the first run left 36 of them unscorable — a mix of UK drill and bass that
    no amount of reasoning about the name can separate. Guessing at them by ear
    is exactly what this project refuses to do everywhere else.

    Order: Stage 9's cache, then Stage 2's cache (free — an artist already
    resolved must never cost a second request), then MusicBrainz. An artist the
    search resolves but leaves untagged falls back to release-group tags, which
    is the pass that gives Tion Wayne his `drill`/`uk drill` and ArrDee his.
    A name MusicBrainz cannot resolve stays unscorable and goes to review.
    """
    from enrich import (MB_MIN_INTERVAL, Throttled, load_cache,
                        resolve_via_musicbrainz, tags_from_release_groups)

    mine = {}
    if CONSOLIDATE_CACHE.exists():
        for line in CONSOLIDATE_CACHE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # truncated final line from an interrupted run
                mine[rec["artist_name"]] = rec

    stage2 = load_cache()
    http, resolved, spent = None, 0, 0
    for name in names:
        rec = mine.get(name) or stage2.get(name)
        if rec is None:
            if http is None:
                http = Throttled(MB_MIN_INTERVAL)
                config.ensure_dirs()
            rec = resolve_via_musicbrainz(http, name)
            if rec.get("mbid") and not rec.get("tags"):
                rec["tags"] = tags_from_release_groups(http, rec["mbid"])
                rec["source"] = "release-groups"
            with CONSOLIDATE_CACHE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            spent += 1
        if rec.get("tags"):
            weights[normalise(name)] = _weights_from_tags(name, rec["tags"])
            resolved += 1
    print(f"  resolved {resolved}/{len(names)} previously untagged artists "
          f"({spent} MusicBrainz lookup(s) spent)")
    return resolved


# --------------------------------------------------------------------------
# Scoring a track — worst credit wins, features at half weight
# --------------------------------------------------------------------------


def track_rap_share(artists: list[str], weights: dict[str, dict]) -> tuple:
    """(rap_share, reason, tag_note) for one track.

    Each credit contributes `rap_share(artist) * credit_weight`, and the track
    takes the maximum. Spotify's `artists` array does not mark who is featured,
    so position is the only signal there is: artists[0] is primary at 1.0, the
    rest are treated as features at CONSOLIDATE_FEATURE_WEIGHT.

    The consequence is deliberate. A rapper guesting on an electronic track
    contributes 1.0 * 0.5 = 0.5, which lands in the review band rather than
    dropping the track outright — a producer's instrumental-leaning collab
    survives to be judged by eye. A rap track under a rap primary still scores
    1.0 and drops. A genuine co-headline is under-weighted by this and that is
    the known cost of the position heuristic.

    The discount is suspended when the PRIMARY artist cannot be scored. It
    exists to stop a guest overruling a known primary; with no known primary
    there is nothing to protect, and applying it anyway means every rap track
    whose lead MusicBrainz has not tagged scores exactly 1.0 * 0.5 = 0.5 and
    lands in review instead of dropping. The first real run produced 15 of those
    in one go — "Dave, Central Cee - Sprinter", "22Gz, Kodak Black - Spin the
    Block" — all plainly rap, all rescued by a discount meant for someone else.

    Returns share=None when no credit could be scored at all, with the reason
    distinguishing "MusicBrainz has never heard of them" from "tagged, but as
    rock" — those need different answers from a human.
    """
    scorable = [normalise(a) in weights
                and weights[normalise(a)]["rap_share"] is not None
                for a in artists]
    primary_known = bool(scorable) and scorable[0]

    scored, unknown, offfam, notes = [], [], [], []
    for i, name in enumerate(artists):
        w = 1.0 if (i == 0 or not primary_known) \
            else config.CONSOLIDATE_FEATURE_WEIGHT
        rec = weights.get(normalise(name))
        if rec is None:
            unknown.append(name)
            continue
        if rec["rap_share"] is None:
            offfam.append(name)
            notes.append(f"{name}: off-family")
            continue
        scored.append(rec["rap_share"] * w)
        notes.append(f"{name}: {rec['rap_share']:.2f}"
                     + (f" [{','.join(rec['tags'][:4])}]" if rec["tags"] else "")
                     + ("" if primary_known else " (full weight: primary untagged)"))

    if not scored:
        if unknown and not offfam:
            return None, f"untagged: {', '.join(unknown)}", "; ".join(notes)
        if offfam and not unknown:
            return None, f"off-family: {', '.join(offfam)}", "; ".join(notes)
        return None, "untagged/off-family mix", "; ".join(notes)
    return max(scored), "", "; ".join(notes)


def decide(share: float | None) -> str:
    if share is None:
        return REVIEW
    if share < config.CONSOLIDATE_KEEP_BELOW:
        return KEEP
    if share > config.CONSOLIDATE_DROP_ABOVE:
        return DROP
    return REVIEW


# --------------------------------------------------------------------------
# Spotify reads
# --------------------------------------------------------------------------


def gentle_token(client_id: str, scope: str) -> str:
    """Reuse the cached access token; refresh only when Spotify rejects it.

    poll.access_token() refreshes unconditionally and Spotify rotates the PKCE
    refresh token when it does. Another process sharing this token file — a
    concurrent poll or Stage 8 run — can have its credential invalidated by a
    refresh it did not ask for. Trying the cached token first costs one cheap
    /me call and removes that whole class of interference. When the token really
    is dead, this falls straight through to the normal path, scope union and all.
    """
    tok = load_tokens()
    if tok.get("access_token") and not missing_scopes(tok, scope):
        probe = Spotify(tok["access_token"]).get("/me", params={})
        if isinstance(probe, dict) and "_status" not in probe:
            return tok["access_token"]
    return access_token(client_id, scope)


def find_playlist(sp, name: str) -> dict:
    """Resolve one playlist by EXACT name, exact including case.

    Same rule as Stage 8's ensure_playlist and for the same reason: a near-miss
    is somebody's hand-made playlist. Here the stakes are lower (this only
    reads) but a wrong source silently consolidates the wrong music, so an
    ambiguous or missing name is a hard error rather than a best guess.
    """
    hits, page = [], sp.get("/me/playlists", params={"limit": 50})
    while isinstance(page, dict) and "_status" not in page:
        hits.extend(p for p in page.get("items", []) if p.get("name") == name)
        nxt = page.get("next")
        if not nxt:
            break
        page = sp.get(nxt.removeprefix(SP_API), params=None)
    if isinstance(page, dict) and "_status" in page:
        raise SystemExit(f"Could not list playlists: {page}")
    if not hits:
        raise SystemExit(f"No playlist named exactly {name!r}.")
    if len(hits) > 1:
        raise SystemExit(
            f"{len(hits)} playlists are named exactly {name!r}; "
            "rename one so there is no doubt which to read.")
    return hits[0]


def read_playlist(sp, pid: str) -> list[dict]:
    """Every track with its added_at and its full credit list.

    Not playlists.playlist_items: that flattens the artists into one string for
    the archive snapshot, and the scoring here needs them as a list to weight
    features separately. `/items` and the `item` key, not `/tracks`/`track` —
    renamed in Spotify's February 2026 release.
    """
    out: list[dict] = []
    resp = sp.get(f"/playlists/{pid}/items"
                  "?fields=items(added_at,item(uri,name,artists(name))),next"
                  "&limit=100", params=None)
    while isinstance(resp, dict) and "_status" not in resp:
        for it in resp.get("items", []):
            t = it.get("item") or {}
            if not t.get("uri"):
                continue  # local files and unavailable tracks carry no URI
            out.append({
                "spotify_track_uri": t["uri"],
                "track_name": t.get("name") or "",
                "artists": [a.get("name", "") for a in t.get("artists", [])],
                "added_at": it.get("added_at") or "",
            })
        nxt = resp.get("next")
        if not nxt:
            break
        resp = sp.get(nxt.removeprefix(SP_API), params=None)
    if isinstance(resp, dict) and "_status" in resp:
        raise SystemExit(f"Playlist read failed part-way: {resp}")
    return out


# --------------------------------------------------------------------------
# Dedupe, overrides, review file
# --------------------------------------------------------------------------


def dedupe_key(row: dict) -> tuple:
    """Folded title + normalised primary artist.

    URI-dedupe alone is not enough: Spotify presses the album cut, the single
    and the remaster as three distinct URIs, so a URI key hands one song several
    slots. _title_key is Stage 8's fold and already handles the case where a
    title is *only* a suffix.
    """
    primary = row["artists"][0] if row["artists"] else ""
    return (_title_key(row["track_name"]), normalise(primary))


def load_overrides() -> dict[tuple, str]:
    """Hand-written answers, keyed the same way tracks are deduped.

    Absent file is normal — the first run has nothing to answer yet. A decision
    that is neither keep nor drop is rejected loudly rather than silently
    ignored, the same way enrich.py refuses an override that is neither a UUID
    nor IGNORE: a typo'd value would otherwise read as a real answer.

    Comment rows are skipped before validation, matching artist_overrides.csv —
    a prose line containing a comma would otherwise parse as a malformed row.
    """
    path = config.CONSOLIDATE_OVERRIDES_CSV
    if not path.exists():
        return {}
    out: dict[tuple, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            decision = (row.get("decision") or "").strip()
            artist = (row.get("artist_name") or "").strip()
            track = (row.get("track_name") or "").strip()
            if not decision or decision.startswith("#") or not artist or not track:
                continue
            if decision.casefold() not in (KEEP, DROP):
                print(f"  ⚠ {path.name} line {lineno}: decision {decision!r} "
                      f"is neither {KEEP} nor {DROP} — skipped")
                continue
            out[(_title_key(track), normalise(artist))] = decision.casefold()
    return out


def write_review(rows: list[dict]) -> None:
    """Everything still needing a human answer, regenerated every run.

    Machine output, deliberately a different file from the hand-written
    overrides — the same split as enrich.py's artist_review.parquet against
    artist_overrides.csv. Copy a row here into consolidate_overrides.csv with a
    decision filled in and it stops coming back.
    """
    config.CONSOLIDATE_REVIEW_CSV.parent.mkdir(parents=True, exist_ok=True)
    with config.CONSOLIDATE_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=REVIEW_COLS)
        w.writeheader()
        for r in sorted(rows, key=lambda r: (-(r["rap_share"] or 0),
                                             r["artists"][0] if r["artists"] else "",
                                             r["track_name"])):
            w.writerow({
                "decision": "",
                "source": r.get("source", ""),
                "artist_name": ", ".join(r["artists"]),
                "track_name": r["track_name"],
                "rap_share": "" if r["rap_share"] is None else f"{r['rap_share']:.3f}",
                "reason": r["reason"] or "ambiguous band",
                "added_at": r["added_at"],
                "tags": r["tag_note"],
                "spotify_track_uri": r["spotify_track_uri"],
            })


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def classify(old: list[dict], weights: dict, overrides: dict) -> list[dict]:
    """Score every track from the older playlist and band it."""
    out = []
    for row in old:
        share, reason, tag_note = track_rap_share(row["artists"], weights)
        verdict = decide(share)
        ov = overrides.get(dedupe_key(row))
        out.append(dict(row, rap_share=share, reason=reason, tag_note=tag_note,
                        verdict=ov or verdict, overridden=bool(ov)))
    return out


def build(whole: list[dict], scored: list[dict]) -> tuple[list[dict], list[dict]]:
    """The consolidated track list, and the rows still needing an answer.

    Playlists taken whole go in first, in the order they were named and each in
    its own internal order; survivors from the filtered playlists follow. Dedupe
    runs across every source and within each, and the first source to claim a
    song keeps it — so a track appearing in both a whole-kept and a filtered
    playlist keeps the whole-kept pressing and position.
    """
    seen, merged = set(), []
    for row in whole:
        k = dedupe_key(row)
        if k in seen:
            continue
        seen.add(k)
        merged.append(row)

    review = []
    for row in scored:
        if row["verdict"] == REVIEW:
            review.append(row)
            continue
        if row["verdict"] != KEEP:
            continue
        k = dedupe_key(row)
        if k in seen:
            continue
        seen.add(k)
        merged.append(row)
    return merged, review


def create_and_fill(sp, name: str, uris: list[str], description: str) -> str:
    """Create the consolidated playlist and add every track, 100 at a time.

    POST /me/playlists, not /users/{uid}/playlists — the per-user endpoints were
    removed in February 2026 and the old path now answers 403.
    """
    created = sp.post("/me/playlists", json={
        "name": name, "public": False, "description": description})
    if not (isinstance(created, dict) and created.get("id")):
        raise SystemExit(f"Could not create playlist {name!r}: {created}")
    pid = created["id"]
    for i in range(0, len(uris), 100):
        chunk = uris[i:i + 100]
        resp = sp.post(f"/playlists/{pid}/items", json={"uris": chunk})
        if isinstance(resp, dict) and "_status" in resp:
            raise SystemExit(
                f"Playlist created ({pid}) but adding tracks failed at "
                f"offset {i}: {resp}\nIt is on your account, partly filled.")
        print(f"  added {min(i + 100, len(uris))}/{len(uris)}")
    return pid


# --------------------------------------------------------------------------
# Report — the verification surface, per CLAUDE.md
# --------------------------------------------------------------------------


def report(whole: list[dict], scored: list[dict], merged: list[dict],
           review: list[dict], sizes: dict[str, int]) -> None:
    print("\n" + "=" * 72)
    print("Stage 9 — consolidation")
    print("=" * 72)

    print("\nsources")
    for name, n in sizes.items():
        mode = "all kept" if any(r["source"] == name for r in whole) else "filtered"
        print(f"  {n:>4}  {name!r} ({mode})")

    if scored:
        verdicts = Counter(r["verdict"] for r in scored)
        overridden = sum(1 for r in scored if r["overridden"])
        print("\nfiltered verdicts")
        for v in (KEEP, DROP, REVIEW):
            print(f"  {v:<8} {verdicts.get(v, 0):>4}")
        print(f"  (of which hand-overridden: {overridden})")

        per_source = Counter((r["source"], r["verdict"]) for r in scored)
        if len({s for s, _ in per_source}) > 1:
            print("\n  by source")
            for name in sizes:
                row = [per_source.get((name, v), 0) for v in (KEEP, DROP, REVIEW)]
                if any(row):
                    print(f"    {name!r:<28} keep {row[0]:>4}  "
                          f"drop {row[1]:>4}  review {row[2]:>4}")

    reasons = Counter(r["reason"].split(":")[0] for r in review if r["reason"])
    if review:
        print("\n  review breakdown")
        for k, n in reasons.most_common():
            print(f"    {k:<28} {n:>4}")
        print(f"    {'ambiguous band':<28} "
              f"{sum(1 for r in review if not r['reason']):>4}")

    kept = Counter(r["source"] for r in merged)
    candidates = len(whole) + sum(1 for r in scored if r["verdict"] == KEEP)
    dupes = candidates - len(merged)
    print("\nconsolidated")
    print(f"  {len(merged)} tracks, {dupes} duplicate(s) folded")
    for name, n in kept.most_common():
        print(f"    {n:>4}  from {name!r}")

    # The drops worth a human's attention are the ones nearest the threshold,
    # not the most obvious ones: a 1.00 is rap by every measure and checking it
    # proves nothing. These are the calls that would flip if the band moved.
    dropped = [r for r in scored if r["verdict"] == DROP and not r["overridden"]]
    if dropped:
        print("\n  closest drops — the riskiest calls, check these are really rap")
        for r in sorted(dropped, key=lambda r: r["rap_share"] or 0)[:10]:
            print(f"    {r['rap_share']:.2f}  "
                  f"{', '.join(r['artists'])[:38]:<38} {r['track_name'][:32]}")

    kept = [r for r in scored if r["verdict"] == KEEP and not r["overridden"]]
    if kept:
        print("\n  closest kept calls (sanity check these are NOT rap)")
        for r in sorted(kept, key=lambda r: -(r["rap_share"] or 0))[:8]:
            print(f"    {r['rap_share']:.2f}  "
                  f"{', '.join(r['artists'])[:38]:<38} {r['track_name'][:32]}")

    if review:
        print(f"\n  {len(review)} track(s) need an answer -> "
              f"{config.CONSOLIDATE_REVIEW_CSV}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep-whole", dest="whole", action="append", default=[],
                    metavar="NAME", required=True,
                    help="playlist to take entire, unfiltered. Repeatable. "
                         "Exact name, case and trailing spaces included.")
    ap.add_argument("--filter", dest="filtered", action="append", default=[],
                    metavar="NAME",
                    help="playlist to genre-filter before merging. Repeatable.")
    ap.add_argument("--name", dest="target", default=None,
                    help="name for the consolidated playlist "
                         "(default: '<first --keep-whole> · consolidated')")
    ap.add_argument("--resolve-missing", action="store_true",
                    help="ask MusicBrainz about artists with no local tags "
                         "(1.1s per lookup) instead of sending them to review")
    ap.add_argument("--write", action="store_true",
                    help="actually create the playlist; without it nothing is written")
    args = ap.parse_args()

    overlap = set(args.whole) & set(args.filtered)
    if overlap:
        sys.exit(f"{', '.join(map(repr, overlap))} is named as both kept-whole "
                 "and filtered; pick one.")

    target = args.target or f"{args.whole[0]} · consolidated"

    if not config.ARTIST_TAGS_PARQUET.exists():
        sys.exit(f"Missing {config.ARTIST_TAGS_PARQUET} — run enrich.py first.")

    client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
    if not client_id:
        sys.exit("SPOTIFY_CLIENT_ID is not set; see .env.example.")

    con = duckdb.connect()
    con.execute("CREATE OR REPLACE VIEW artist_tags AS SELECT * FROM "
                f"'{config.ARTIST_TAGS_PARQUET}'")
    weights = artist_family_weights(con)
    print(f"  {len(weights)} artists carry tags locally")

    sp = Spotify(gentle_token(client_id, SCOPES_WRITE if args.write else SCOPES_READ))

    sizes, whole, to_filter = {}, [], []
    for name in args.whole + args.filtered:
        pl = find_playlist(sp, name)
        print(f"  reading {name!r} ...")
        rows = [dict(r, source=name) for r in read_playlist(sp, pl["id"])]
        sizes[name] = len(rows)
        (whole if name in args.whole else to_filter).extend(rows)

    if args.resolve_missing:
        # Only names that actually matter: an artist on a track already answered
        # by hand needs no lookup, and neither does one already tagged.
        overrides_now = load_overrides()
        unknown = []
        for row in to_filter:
            if dedupe_key(row) in overrides_now:
                continue
            for name in row["artists"]:
                if normalise(name) not in weights and name not in unknown:
                    unknown.append(name)
        if unknown:
            print(f"  {len(unknown)} artist(s) with no local tags; "
                  f"resolving (~{len(unknown) * 1.1:.0f}s) ...")
            resolve_missing(unknown, weights)

    overrides = load_overrides()
    if overrides:
        print(f"  {len(overrides)} hand-written override(s) loaded")

    scored = classify(to_filter, weights, overrides)
    merged, review = build(whole, scored)
    write_review(review)
    report(whole, scored, merged, review, sizes)

    if not args.write:
        print(f"DRY RUN — nothing written to Spotify. Would create {target!r} "
              f"with {len(merged)} tracks.")
        print("Re-run with --write once the calls above look right.\n")
        return

    print(f"Creating {target!r} with {len(merged)} tracks ...")
    pid = create_and_fill(sp, target, [r["spotify_track_uri"] for r in merged],
                          f"{', '.join(args.whole)} in full, plus the non-rap "
                          f"parts of {', '.join(args.filtered)}. "
                          f"Built by spotify-trend-analysis.")
    print(f"\nDone: {target!r} ({pid}). Every source is untouched.\n")


if __name__ == "__main__":
    main()
