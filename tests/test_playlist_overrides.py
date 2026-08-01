"""tests/test_playlist_overrides.py — the file where a human overrules the gaps.

The gap ranking weights by seconds listened, and seconds are dominated by
whatever plays during a workout. The export records no activity type, so it
cannot tell functional listening from attentive listening. This file is where
that missing fact gets supplied — so it has to be read exactly right.
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, ".")
import duckdb
import playlists

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


con = duckdb.connect()
con.execute("""
CREATE TABLE genre_gaps AS SELECT * FROM (VALUES
  ('dubstep', 0.0005, 30.0, 81, 0.91), ('heavy metal', 0.0003, 4.9, 16, 1.27),
  ('techno', 0.0004, 8.7, 38, 1.32), ('rap rock', 0.0007, 12.3, 13, 1.01),
  ('edm', 0.0002, 98.7, 160, 0.50)
) t(tag, gap_score, hours, n_artists, rel_change_per_year)""")

tmp = pathlib.Path(tempfile.mkdtemp())
csv_path = tmp / "playlist_overrides.csv"
playlists.config.PLAYLIST_OVERRIDES_CSV = csv_path

# --------------------------------------------------------------------------
# No file: fall back to the gap ranking, unchanged behaviour.
# --------------------------------------------------------------------------
specs = playlists.load_playlist_specs(con)
check("no override file -> top gaps by score",
      [s["label"] for s in specs], ["rap rock", "dubstep", "techno", "heavy metal"])
check("fallback specs are single-tag", [s["tags"] for s in specs],
      [["rap rock"], ["dubstep"], ["techno"], ["heavy metal"]])
check("fallback specs are never pinned", {s["pinned"] for s in specs}, {False})
check("fallback specs carry their gap stats",
      all(s["gap"] and s["gap"]["n_artists"] for s in specs), True)

# --------------------------------------------------------------------------
# With a file: it decides everything, including genres the gaps never ranked.
# --------------------------------------------------------------------------
csv_path.write_text(
    "label,tags\n"
    "dubstep,dubstep|future bass|drum and bass\n"
    "heavy metal,heavy metal\n"
    "indie,indie pop|indie folk\n"
    "indie rock,indie rock\n", encoding="utf-8")

specs = playlists.load_playlist_specs(con)
check("override file replaces the gap ranking",
      [s["label"] for s in specs], ["dubstep", "heavy metal", "indie", "indie rock"])
check("pipe-separated tags become a blend",
      specs[0]["tags"], ["dubstep", "future bass", "drum and bass"])
check("a bare label is its own single tag", specs[1]["tags"], ["heavy metal"])
check("a label in genre_gaps is NOT pinned", specs[0]["pinned"], False)
check("a label absent from genre_gaps IS pinned", specs[2]["pinned"], True)
check("gap-derived specs keep their trend numbers",
      specs[0]["gap"]["n_artists"], 81)
check("pinned specs have no trend numbers to keep", specs[2]["gap"], None)
check("dropped genres are gone entirely",
      any(s["label"] in ("techno", "rap rock") for s in specs), False)

# A widened gap genre stays gap-derived: dubstep is still rising, the extra
# tags only widen where its material comes from.
check("widening a gap genre does not make it pinned", specs[0]["pinned"], False)

# --------------------------------------------------------------------------
# Robustness — this is a hand-edited file.
# --------------------------------------------------------------------------
csv_path.write_text(
    "label,tags\n"
    "# a comment row,ignored\n"
    "  spaced  ,  indie pop  |  indie folk  \n"
    ",orphan tags with no label\n"
    "bare label,\n", encoding="utf-8")
specs = playlists.load_playlist_specs(con)
check("comment rows skipped", any(s["label"].startswith("#") for s in specs), False)
check("blank labels skipped", [s["label"] for s in specs], ["spaced", "bare label"])
check("whitespace trimmed around tags", specs[0]["tags"], ["indie pop", "indie folk"])
check("empty tags fall back to the label", specs[1]["tags"], ["bare label"])

# More rows than N_PLAYLISTS: capped, not silently obeyed.
csv_path.write_text("label,tags\n" + "".join(
    f"g{i},tag{i}\n" for i in range(9)), encoding="utf-8")
specs = playlists.load_playlist_specs(con)
check("row count capped at N_PLAYLISTS", len(specs), playlists.config.N_PLAYLISTS)

# A file with a header and nothing else means "no playlists", not "fall back to
# gaps" — an empty deliberate answer is still an answer.
csv_path.write_text("label,tags\n", encoding="utf-8")
check("empty override file yields no playlists",
      playlists.load_playlist_specs(con), [])

# Excel and some editors write a BOM; it must not corrupt the first label.
csv_path.write_text("﻿label,tags\ndubstep,dubstep\n", encoding="utf-8")
check("BOM does not corrupt the first label",
      [s["label"] for s in playlists.load_playlist_specs(con)], ["dubstep"])

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
