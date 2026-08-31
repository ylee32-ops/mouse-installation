# Scenes — what plays, in what order, and when it changes

Everything is controlled by **`scenes.json`**. You shouldn't need to edit
`mouse_video.py` to change how the piece behaves.

```
photos in frames/  ─►  ../v2/prototype.html  ─►  scenes.json  ─►  build_scenes.py  ─►  mouse_video.py
   (shoot)              (tune, export)      (the truth)       (encode)            (run on the Pi)
```

---

## 1. The sequences

| # | Name | What it is | Loops? | Reacts to sound? |
|---|---|---|---|---|
| **0** | base | the wall, no mouse | yes (a single still is fine) | resets the quiet timer |
| **1** | out | comes out of the hole | no | **yes — rewinds** |
| **2** | loop | stays out | yes | **yes — goes in** |
| **3** | in | goes back in | no | no |
| **4** | easter egg | the dogs | no | **no, not at all** |
| **5** | loading | startup | no | no |

```
frames/
  base/            0   one still photo is enough
  loading/         5
  easter/
    dog_left/      4
    dog_right/     4
  <character>/
    out/           1   or 1_out  — a leading number is taken at its word
    loop/          2   or 2_loop
    in/            3   or 3_in   — optional; omit it and 1 is rewound
```

Folder names are matched loosely (`coming_out`, `nibbling`, `going_back` all
resolve correctly), but a leading `0`–`5` always wins. Photo numbering can be
anything consistent — they sort numerically, so `frame_2` precedes `frame_10`.

---

## 1b. Where the photos live

The stills are ~8 MB each and 2 GB in total, so they don't live in the repo.
Point `scenes.json` at wherever they actually are:

```json
"frames_root": "/Volumes/JuJu/_SVA/mouse-installation"
```

Every `"frames": "frames/..."` path is then resolved from there. Leave it out and
paths resolve next to the scripts instead. If the drive isn't mounted,
`build_scenes.py` says so instead of reporting a hundred missing folders.

### Two copies of the photos

| Folder | What | Used by |
|---|---|---|
| `frames/` | the originals, 3024×2016 | `build_scenes.py` → the Pi. This is where final quality comes from. |
| `frames_preview/` | same tree, same filenames, 960px wide | `../v2/prototype.html` only |

The prototype holds every frame in memory at once; at full resolution that is
~6.5 GB of decoded bitmap and the browser falls over. The previews are ~4% of
that.

**This costs the code nothing.** The prototype strips whichever folder you
picked and rebuilds the paths as `frames/...`, so loading `frames_preview/`
exports exactly the same `scenes.json` as loading `frames/` would. Tune against
the previews, export, build from the originals. Nothing to keep in sync, and no
setting to remember to change back.

The previews stay **uncropped 3:2**, so the pad/crop comparison in §3b still
shows you the truth.

If you reshoot a sequence, regenerate that folder's previews:

```bash
cd /Volumes/JuJu/_SVA/mouse-installation
find frames -name '*.jpg' -not -name '._*' | sed 's|^frames/||' \
  | xargs -P 8 -I{} sips -Z 960 "frames/{}" --out "frames_preview/{}"
```

---

## 2. The state machine

```
                                    ┌──────────────┐
        startup ────────────────────│  5 LOADING   │
                                    └──────┬───────┘
                                           ▼
     ┌──────────────────────────────► ┌─────────┐
     │                                │ 0 BASE  │◄── quiet timer runs here
     │                                └────┬────┘
     │                     quiet_seconds   │   every n-th time
     │                      ┌──────────────┴──────────────┐
     │                      ▼                             ▼
     │                 ┌─────────┐                 ┌──────────────┐
     │                 │  1 OUT  │                 │ 4 EASTER EGG │ deaf
     │                 └────┬────┘                 └──────┬───────┘
     │            sound ────┤                             │
     │              ▼       │ ends                        │
     │        ┌──────────┐  ▼                             │
     │        │ 1R REWIND│ ┌─────────┐                    │
     │        └────┬─────┘ │ 2 LOOP  │◄── loops here      │
     │             │       └────┬────┘                    │
     │             │   sound ───┤                         │
     │             │            ▼                         │
     │             │       ┌─────────┐                    │
     │             │       │  3 IN   │ deaf               │
     │             │       └────┬────┘                    │
     │             └────────────┴─────────────────────────┘
     │                          ▼
     │                   ┌────────────┐
     └───────────────────│  COOLDOWN  │ nobody may come out
                         └────────────┘
```

**Normal order:** `0 - 1 - 2 (loops until sound) - 3 - 0`
**Easter egg:** `0 - 4 - 0`  ·  **Startup:** `0 - 5 - 0`

**Time from a noise to the next mouse** = `cooldown_seconds` (nobody at all)
**+** `quiet_seconds` of *unbroken* quiet. Any sound in that second window
resets it. With the defaults, 6 + 20 = **26 seconds minimum**.

---

## 3. What a sound does, state by state

### During **0 base** — nothing
The quiet timer resets. That's all.

### During **1 out** — stop and rewind (`1 - 1R - 0`)
It does **not** jump to 3; that reads as a teleport. Instead the out sequence is
played backwards, and it picks up at the **mirrored frame**:

> the out sequence is 1.20s long, the sound lands 1.02s in → the rewind starts
> at 0.18s, so only the last 0.18s of retreat is left to play

The mouse turns around exactly where it was standing. Speed is `rewind_speed`
(default 1.5×) — a little quicker than coming out, since it's startled.

### During **2 loop** — go in (`2 - 3 - 0`)
The common case. Two ways to hand over, set by `timing.on_sound_during_loop`:

| Mode | What happens | Use when |
|---|---|---|
| **`finish_fast`** *(default)* | Plays out the rest of the current loop cycle at `loop_exit_speed` (3×), then starts 3 | Your loop moves a lot, and 3 was shot starting from the loop's resting pose |
| **`cut`** | Jumps to 3 immediately | Your loop barely moves, so any frame is a fine hand-off — sharper, more startling |

Try both in the prototype; which one looks right depends entirely on how much
your loop moves.

### During **3 in** — nothing
It is already leaving, at its normal speed.

### During **4 easter egg** — nothing at all
Sound is ignored, *including for the quiet timer*. See §5 for why that matters.

---

## 3b. Screen shape — pad or crop

The photos are **3:2** (3024×2016). Most screens are **16:9**. Something has to
give, and it's about **16%** either way:

```json
"display": { "width": 1920, "height": 1080, "fit": "pad" }
```

| `fit` | What happens | Costs |
|---|---|---|
| **`pad`** *(default)* | The whole photo is kept, centred | Black bars down the sides, ~16% of the screen |
| **`crop`** | The photo fills the screen | ~16% lost off the top and bottom of every frame |

**This decision can wait until the very end.** Nothing is baked in until you
encode: the originals stay full-frame 3:2, and switching `fit` just means
re-running `build_scenes.py`. No reshoot, no re-import.

To judge it, open `../v2/prototype.html` and use the **Screen** panel — flip between
`pad` and `crop` while a sequence is playing and it re-renders live, telling you
exactly what each one costs. If your screen isn't 1920×1080, set the real
resolution there first; the trade-off changes with the screen's shape (on a 3:2
screen there is no trade-off at all).

---

## 4. Speed

| Control | Where | What it does |
|---|---|---|
| `fps` | per sequence | The **baked** stop-motion cadence. 6–8fps looks handmade, 12–15 smooth. Changing it needs a rebuild. |
| `rewind_speed` | global, per-character override | Speed of 1R. Default 1.5×. |
| `loop_exit_speed` | global | How fast the loop finishes its last cycle before 3, in `finish_fast` mode. |

Sequence 3 always plays at its shot speed — per your spec, no change needed.

---

## 5. Easter eggs

The dogs **take the slot a mouse would have used**: when the quiet timer expires
and an egg is due, the dog comes out instead. No loop, no in/out, no speed
change, no sound reaction.

```json
"easter_eggs": {
  "every": [4, 8],
  "order": "alternate",
  "post_quiet_seconds": 8.0
}
```

- **`every`** — a random gap in this range between appearances, counted in
  emergences. Fixed numbers (`[6, 6]`) make it exactly every 6th.
- **`order`** — `alternate` runs dog 1, dog 2, dog 1… so both get seen.
  `random` can repeat the same dog twice.
- **`post_quiet_seconds`** — **this one is easy to miss.** People *react* to the
  dog: they laugh, they call someone over. Since sound during the egg is
  ignored, the quiet timer starts fresh when the dog leaves — but the room is
  still noisy, so with the normal 20s the next mouse could be a long time
  coming, right after your most engaging moment. This shortens the wait just
  after an egg. Set it equal to `quiet_seconds` to turn the behaviour off.

---

## 6. Thresholds — when does it react

```json
"auto_threshold": true, "noise_margin": 3.0, "min_threshold": 0.015,
"flee_hits_needed": 3, "flee_hit_window": 0.35
```

With `auto_threshold` on, the program measures the kitchen's noise floor over a
rolling 30 seconds and reacts at `noise_margin` × that. This is the fix for a
threshold tuned in one room leaving the mouse permanently hidden in another —
the fridge and the extractor fan get learned, not fought.

- **Never comes out** → check `~/mouse.log`: `thr=` is the live threshold, `vol=`
  the current level. If `vol` sits near `thr`, raise `noise_margin`.
- **Reacts to nothing** → raise `noise_margin` (try 4.0) or `flee_hits_needed`.
- `flee_hits_needed` / `flee_hit_window` — how many separate 50ms mic readings
  in a short window must be loud. `3` in `0.35s` ignores a chair scrape but
  catches a spoken word.

Set `"auto_threshold": false` to go back to the fixed `threshold`.

---

## 7. Cheat-sheet

| Want | Change |
|---|---|
| Mouse appears sooner | `timing.quiet_seconds` down |
| One character is bolder | that character's `quiet_seconds` |
| Longer empty pause after a retreat | `timing.cooldown_seconds` up |
| A character shows up more often | its `weight` up |
| Stop-motion too smooth | that sequence's `fps` down (6–8) |
| Retreat too casual | `rewind_speed` up |
| Dogs too rare / too frequent | `easter_eggs.every` |
| Loop→in hand-off jumps the pose | `on_sound_during_loop: "finish_fast"` |
| Loop→in feels sluggish | `"cut"`, or `loop_exit_speed` up |

---

## 8. Adding a character

1. Shoot into `frames/<name>/` as `1_out`, `2_loop`, and optionally `3_in`.
2. Open `../v2/prototype.html`, **Load frames folder** — it appears automatically.
3. Tune, then **Export scenes.json** into the repo.
4. `python3 build_scenes.py && python3 mouse_video.py`

Sequence 3 is optional throughout. Omit it and the character goes back in on its
rewound out sequence, which for many mice is indistinguishable and saves a shoot.
