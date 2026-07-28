# BRASS TACKS — BUILD SPECIFICATION v1

**THE NIGHT DESK**, with grafts. Handed to an engineer. React 19 + Vite + plain CSS, no chart library, no component kit. Work top to bottom; every decision is made for you.

Everything below is checked against `frontend/src/fixtures/demo.json` (the real export from the live cluster) and `frontend/src/types.ts`. Where the brief and the data disagree, the data wins and I say so.

---

## 0. THE CALL ON WHICH DIRECTION WINS

Combined scores tied THE NIGHT DESK and Ninety Seconds at 22.2. **Night Desk is the spine.** Reasons, stated so you can argue with them:

- Rank profile: Night Desk placed 1 / 2 / 4 (mean 2.33); Ninety Seconds placed 3 / 4 / 2 (mean 3.00). Night Desk has the higher peak and the better floor.
- The brand judge — the one whose criterion maps to the two rejections ("not on par with professional companies") — put Night Desk first, and the reason generalises: *a publication is the only one of the five framings that natively wants a twelve-line paragraph.* The crux constraint is answered by the identity rather than fought by it.
- Ninety Seconds' fatal risk was structural (its emptiness is indistinguishable from the emptiness already rejected twice, with no fallback). Night Desk's fatal risk was a **single encoding choice** — ink-density-as-confidence — which is replaceable without touching the architecture. Fixable beats unfixable.

Night Desk loses three things on the way in: **ink-density**, **the Dawn animation**, and **the Sixty-One Nights chart**. All three are replaced below and listed in §15.

Grafted, per the judges:
| From | What | Where it lands |
|---|---|---|
| ASSAY (bench) | The work-order header that prices *her* workload before she reads a step | §7, THE WORK ORDER — rewritten in plain sentence case, and corrected against the data (see §1.4) |
| ASSAY (bench) | Guardrail clauses lifted out of the prose, fixed position above the fold | §7, THE ASK + "What I won't do without you" |
| ASSAY (bench) | Clamp is CSS; full text always in the DOM; `Read it as written · N words` | §7 |
| Ninety Seconds | Refusing the growth chart: "Two months of ledger. A line needs six." + `2 / 6` | §9.2 |
| Board & Book | Hatch and **label** the gap so empty space reads as the job, not as absent data | §9.1 |
| Board & Book | Evidence mark never issues a verdict; it always prints its plain-language equivalent | §8 |
| Stamped Record | "6 of 7 calls right", never a bare percentage | §12 |
| Ninety Seconds | Brass darkened for small text (`#A8781F` is 3.59:1 — illegal under 18px) | §3 |

---

## 1. CONTESTED DECISIONS — RESOLVED

Read this section before writing code. Every one of these was raised by a judge and every one changes what you build.

### 1.1 Ink density as confidence — **KILLED**

The buildability judge is right and it is worse than he said. Real distribution across all 108 evidence rows in the fixture:

```
n=108   min −0.0003   p25 0.237   median 0.308   p75 0.360   p90 0.429   max 0.702
≥ 0.45: 10 rows (9%)        < 0.30: 50 rows (46%)
```

Night Desk's spec renders 0.31 at 46% ink and 0.12 at 28% ink. Against this distribution, **the majority of every evidence margin would render at roughly 1.9:1 to 3:1 on paper.** The product's receipts would be its least readable text. Killed outright.

**Replacement:** all margin text is full ink, always. Strength is carried by a **2px weight rule** under each note's header (§8), and by rank order. Text contrast is never a data channel anywhere in this product.

### 1.2 The evidence floor — **KILLED, and no threshold replaces it**

Every direction proposed a printed floor (0.30 or 0.45). All of them are falsified:

- `9e80b95c` "Reply to every recent low review" is a **VERIFIED WIN** whose evidence tops out at **0.064**, low **0.000**.
- `a0c04c2f` is a **VERIFIED WIN** topping out at **0.157**.
- The published **MISS** (`a2f52fd0`) tops out at **0.315** — *higher* than `00846dff`, a live accepted find at 0.311.

Retrieval similarity does not predict the verdict in this data. Any floor line would mark verified wins as failures. **There is no floor, no pass/fail line, no red hollow ticks, no "conviction threshold."**

**What actually discriminates is COUNT.** Evidence counts across all 17 finds, sorted:

```
2, 4, 4, 5, 5, 5, 5, 5, 6, 7, 7, 8, 8, 8, 9, 9, 11
```

The miss is the only find in the corpus written from fewer than four memories. So the post-mortem line is data-true and needs no cosine literacy:

> **This one was written from 2 memories. Every other find here stood on at least 4.**

That sentence is generated, not hand-written: `min(evidence.length of all other finds)`. It is the honest replacement for the brief's cherry-picked "0.31, 0.12 versus 0.70" story, which is true of exactly one pair.

### 1.3 The $870 / $3,335 arithmetic — **THE BRIEF IS RIGHT; the judge summed the wrong set**

The buildability judge said undecided finds total ~$3,210/mo, not $870, and that the brief's proportions were fiction. He summed `verdict === null` (6 finds). The correct semantics is **status-based**, and under it the brief's arithmetic closes to the cent:

| Bucket | Rule | Fixture | /month |
|---|---|---|---|
| Waiting on you | `status ∈ {proposed, later}` | 2 finds, 2 900 ¢/day | **$870** |
| Earned, verified | `summary.verified_daily_cents` | 12 650 ¢/day | **$3,795** |
| Still to find | `goal − earned − waiting` | — | **$3,335** |
| **Goal** | `business.goal_monthly_cents` | 800 000 ¢ | **$8,000** |

`379 500 + 87 000 + 333 500 = 800 000`. Exact.

**But: never hardcode the proportions.** §9.1 specifies the overflow rule for when brass exceeds green, because it will.

The other two buckets — 4 accepted-but-unmeasured ($2,340/mo) and 4 `estimated` ($3,918/mo) — are **not drawn on the road bar at all**, because neither is counted money. They are listed in words underneath. See §9.1.

### 1.4 "5 STEPS · 1 IS YOURS" — **grafted, but it is FALSE as written**

Both the owner judge and the brand judge picked this as the single best idea in the pile. I checked it against every enumerated move in the fixture. **In all 5 enumerated moves, zero enumerated items belong to Rosa.** Worked example, `33d26b20`:

```
lede : "…I will draft, for your OK before anything goes live or sends:"
(1)  : a host script that captures name, party size…        → agent
(2)  : three SMS templates…                                  → agent
(3)  : a small doorway card…                                 → agent
(4)  : a rule that any party quoted over 45 minutes…         → agent
tail : "Nothing sends and no price or menu changes without your approval."
```

ASSAY's header would print `4 STEPS · 0 ARE YOURS`, which reads as a bug. The real structure of every production move is: *everything in the list is the agent's; the only thing that is hers is the approval gate.*

**The graft, corrected**, in plain sentence case as the owner judge asked:

```
yours === 0  →  "Four things I'll draft. None of them are yours to do."
yours === 1  →  "Five steps. Four are mine, one is yours."
yours  >  1  →  "Five steps. Three are mine, two are yours."
```

Same four-second payload, true against the data, and it degrades correctly if the Analyst's phrasing ever changes.

### 1.5 Footnote superscripts — **restricted, never fabricated**

8-hex observation IDs appear in the running text of only **3 of 17** rationales. Printing superscripts on the other 14 would be invented provenance in a product whose promise is receipts.

**Rule:** margin notes are always numbered `1…n`. Inline superscripts in the prose appear **only** where a literal `[0-9a-f]{8}` matched a real `evidence[].observation_id`. Zero matches → zero superscripts, and the margin is headed *"What he read"* rather than *"Sources"*. This is an `if`, not a feature flag.

### 1.6 Title colon-splitting — **BANNED**

Exactly **1 of 17** titles contains a colon (`8629ea6d`, the one quoted in the brief). Four directions built layout on it. Title lengths run **13 → 98 chars**.

**Rule:** the title is one block, set whole, never split into two type sizes by punctuation, **never truncated at any length**. It wraps. At 4+ rendered lines it steps down one size, once. That is the entire rule. (If a colon happens to be present you may set the post-colon clause at weight 400 against 500 — a weight change inside one type size, no layout consequence, safe when absent.)

### 1.7 `evidence[].rank` is not similarity order — **sort on similarity**

In `8629ea6d`, rank 0 = 0.312 while rank 4 = 0.512. The display promise is "strongest first", so **display order is `similarity` descending**. `rank` is retained on the wire (it is the DB's retrieval order) and is not used for display. Do not change the API.

### 1.8 Parser diagnostics — **never shown to Rosa**

The owner judge called ASSAY's on-screen `AUTO-SPLIT` marker a fatal flaw: *"the software admitting to me that it's guessing at its own writing."* Correct. Every parse fallback is reported via `console.warn` and a `data-parse-mode` attribute for tests. **Nothing about the parser is ever rendered.**

### 1.9 Typeface money vs. counted money — **one rule, no homework**

Board & Book proposed predicted money in a drawn serif and counted money in mono. The owner judge rejected it as a rule she has to hold in her head. **All money everywhere is mono.** Predicted vs. counted is distinguished by the *word* beside it (`if it holds` / `verified`), never by letterform.

### 1.10 Commercial typefaces — **refused**

Ogg, Signifier and Söhne were all specified with free fallbacks, which means the shipped product is the fallback — and Ninety Seconds' fallback was Inter, the exact default it argued against. All three families here are free and self-hosted. The brand judge's note ("buy a display face with an owner") is answered in §4 by *what* is used, not by *what is licensed*.

### 1.11 The dark ground — **kept, but it is chrome only**

The owner judge: *"the whole app being near-black is a lot at 7am with the kitchen lights on."* She is right about the reading surface and wrong about the frame. **Everything Rosa reads is on warm paper.** Dark appears as: the 52–60px masthead band, the left rail, and the 12–16px gutters between sheets. On 375px, paper covers **≥ 88% of the content area**. The dark exists to give the page the focal point the rejected render lacked (one lit sheet), and to make brass findable at arm's length.

**No theme toggle.** The verdict palette is validated against paper; a second surface doubles the QA and the product already contains two materials.

### 1.12 The masthead's 3am theatre — **demoted**

Owner judge: *"It's telling me how hard it worked while I slept, and that is one half-step from a pitch."* The issue number stays (the brand judge's best-reframe pick and the sparse-history fix). The filing time moves off the masthead to a single quiet byline at the foot of the sheet, where a byline belongs.

---

## 2. DATA CONTRACT

No new backend fields are required. Everything below is derived on the client from the existing `DemoData` shape.

### 2.1 Endpoints

```
GET  /api/board                    → DemoData          (exact shape of demo.json)
POST /api/finds/:id/status         { status: FindStatus } → Find
```

Until the API exists, `import demo from './fixtures/demo.json'` behind a `useBoard()` hook with the same signature. Swapping is a one-line change.

### 2.2 Derived selectors — `src/lib/board.ts`

```ts
// ── the four buckets ────────────────────────────────────────────────
waitingOnYou   = finds.filter(f => f.status === 'proposed' || f.status === 'later')
inTheWorks     = finds.filter(f => f.status === 'accepted' && f.verdict === null)
measuring      = finds.filter(f => f.verdict === 'estimated')
judged         = finds.filter(f => f.verdict === 'verified' || f.verdict === 'miss')

// ── tonight's find: highest value, unseen before deferred ───────────
todaysFind =
  byValueDesc(finds.filter(f => f.status === 'proposed'))[0] ??
  byValueDesc(finds.filter(f => f.status === 'later'))[0] ??
  null                                     // → EMPTY STATE (§10.5)

// ── money (all integer cents; never float-accumulate) ───────────────
earnedMo     = summary.verified_daily_cents * 30
waitingMo    = sum(waitingOnYou, 'predicted_daily_cents') * 30
goalMo       = business.goal_monthly_cents ?? 0
toFindMo     = Math.max(0, goalMo - earnedMo - waitingMo)

// ── the record line, as a fraction, never a percentage ──────────────
callsRight   = `${summary.verified} of ${summary.judged} calls right`   // "6 of 7"

// ── the issue number: nights this ledger has existed ────────────────
issueNo      = daysBetween(corpus.earliest, today) + 1                  // fixture → 57

// ── evidence, display order ─────────────────────────────────────────
notes        = [...find.evidence].sort((a,b) => b.similarity - a.similarity)

// ── the miss comparison line (§1.2) ─────────────────────────────────
floorCount   = Math.min(...finds.filter(f => f.id !== miss.id).map(f => f.evidence.length))
```

### 2.3 Kind labels — the emoji replacement

`find.emoji` is **never rendered.** It is tofu on Windows Chrome and it carries less information than the data already has. The kicker is built from the evidence kinds:

```
review → "reviews"  ·  trend → "local trends"  ·  rival_price → "rival prices"
rival_menu → "rival menus"  ·  social → "social posts"
```

Take the top two kinds by count in *this find's* evidence:
`"From 4 reviews and 4 local trends"` · `"From 5 reviews"` · `"From 11 reviews and 2 local trends"`.

---

## 3. COLOUR

Surface for all validation: **paper `#F7F5EF`**.

### 3.1 Materials

| Token | Hex | Role |
|---|---|---|
| `--desk` | `#11151C` | Application ground, masthead band, gutters. Full bleed. |
| `--desk-raised` | `#181D26` | Left rail interior; blocks on the desk. |
| `--desk-rule` | `#262C37` | 1px rules on the desk. |
| `--paper` | `#F7F5EF` | The sheet. Everything Rosa reads. |
| `--paper-dim` | `#EFEDE6` | Inset blocks inside a sheet (the ask bar, the guardrail block). |
| `--rule` | `#E3DFD3` | 1px rules on paper. |
| `--rule-strong` | `#CFC9B8` | Section rules, table rules, chart axes. |

### 3.2 Ink

| Token | Hex | On paper | Role |
|---|---|---|---|
| `--ink` | `#1A1F28` | 15.16:1 | Titles, the money figure, everything primary. |
| `--ink-2` | `#414A59` | 8.20:1 | Body prose, step text, margin quotes. |
| `--ink-3` | `#626A7A` | 4.99:1 | Standing heads, dates, captions, source labels. Floor for text. |
| `--on-desk` | `#CBD2DE` | 12.03:1 on desk | Rail and masthead text. |
| `--on-desk-3` | `#949DAE` | 6.70:1 on desk | Rail captions, inactive nav. |

`--ink-3` was moved from `#6B7383` (4.37:1 — fails) to `#626A7A`. It is the darkest tone in the product that may carry text.

### 3.3 The four semantic roles

| Role | Hex | Contrast on paper | Shape (mandatory) | Word (mandatory) |
|---|---|---|---|---|
| **Verified** | `#118066` | 4.48:1 | filled disc | `Verified` |
| **Miss** | `#C0442A` | 4.70:1 | open square, diagonal strike | `Miss` |
| **Measuring** | `#2E5EA8` | 5.87:1 | ring closed by a dotted arc | `Measuring` |
| **Waiting on you** | `#A8781F` | 3.59:1 | outlined diamond + 45° hatch | `Waiting on you` |

Small-text variants — the four hexes above are **marks and fills only**. Any of these roles set below 18px uses:

| Token | Hex | On paper |
|---|---|---|
| `--verified-ink` | `#0E6B55` | 5.92:1 |
| `--miss-ink` | `#A63A22` | 5.93:1 |
| `--measuring-ink` | `#27528F` | 7.16:1 |
| `--brass-ink` | `#8A6114` | 5.07:1 |
| `--brass-on-desk` | `#D9A94F` | 8.48:1 on desk |

Each variant is a pure lightness step along the same hue, so the pairwise separations below are preserved rather than re-mixed.

### 3.4 Colourblind defensibility — **computed, and one real failure found**

Run with the dataviz validator (`scripts/validate_palette.js`, surface `#F7F5EF`, `--pairs all`). Numbers, not opinion:

**The three verdict colours, as one categorical channel — PASSES every check.**
```
[PASS] Lightness band      all 3 inside L 0.43–0.77
[PASS] Chroma floor        all 3 >= 0.1
[PASS] CVD separation      worst #C0442A↔#118066  ΔE 10.0 (deutan)
[PASS] Normal-vision floor worst #2E5EA8↔#118066  ΔE 16.8
[PASS] Contrast vs surface all 3 >= 3:1
→ ALL CHECKS PASS
```

**Verified ↔ brass, the only two fills on the road bar — PASSES.**
```
[PASS] CVD separation      ΔE 8.9 (protan) · tritan 20.3
[PASS] Normal-vision floor ΔE 17.3
→ ALL CHECKS PASS
```

**All four together as one categorical channel — FAILS. Nobody caught this.**
```
[FAIL] CVD separation      #A8781F↔#C0442A  ΔE 4.8 (deutan) · 5.8 (tritan)
[FAIL] Normal-vision floor #A8781F↔#C0442A  ΔE 12.5 — below 15
```

Brass and miss are neighbouring hues. No brass exists that is still brass and clears the floor against `#C0442A`; I tested seven, and the only one that passed the normal-vision check (`#96820C`) is an olive, not brass, and still failed CVD.

**The fix is structural, not chromatic — and it is a hard architectural constraint:**

> **Brass and miss never occupy the same encoding channel.**
>
> 1. The road bar carries exactly two fills: verified and brass. (Validated pair, passes.)
> 2. The record carries exactly three fills: verified, miss, measuring. (Validated trio, passes.)
> 3. **"Waiting on you" finds do not appear in the record at all.** The record is what happened; the inbox is not. This is also correct product-wise, and it is what makes the palette legal.
> 4. Brass never appears as a chart mark on any plot that carries a verdict mark.
> 5. Every verdict instance, everywhere, carries its distinct **shape** and its **spelled word**. The palette is the third signal, not the first.

Residual: verified ↔ measuring is ΔE 5.8 under tritanopia (~0.01% prevalence). Accepted, because those two never appear as unlabelled adjacent fills — the record lists them as rows with shape and word. If you want it gone, `#2E5EA8 → #28518F` lifts tritan to 10.5 with all other checks still passing; I did not take it because the brief locked `#2E5EA8` and the risk of re-opening a settled palette exceeds the benefit at that prevalence.

### 3.5 Everything else

There is no fifth colour. No brand colour, no hero gradient, no tinted card, no accent. **Brass appears if and only if something is waiting on Rosa's decision** — including in the wordmark, which therefore has none.

Two fills only, both derived, never eyeballed:
- `--brass-hatch` — 45° stripes, `#A8781F` at 100%, 3px on / 5px off, on `--paper-dim`.
- `--wash-cited` — `#A8781F` at 12% over paper, the highlight under a cited sentence in the reading view. The only non-verdict fill in the product.

---

## 4. TYPE

Three families. Each has exactly one job, and the split is semantic — Rosa never learns the rule, she absorbs it in three mornings and can then tell from across the kitchen which parts of the screen are an argument and which are a fact.

| Family | Voice | Weights | Where |
|---|---|---|---|
| **Newsreader** (variable, `opsz` 6–72, `wght` 200–800, true italic) | The analyst's | 400, 500; italic 400 | Everything he wrote: titles, lede, steps, rationale, margin quotes, reading view |
| **Archivo** (variable, `wght` 100–900, `wdth` 62–125) | The building's | 500, 600 @ `wdth 112` | Everything the software says: standing heads, nav, buttons, labels, captions |
| **Commit Mono** (`tabular-nums` default) | The money's | 400, 500, 600 | Every figure: money, dates, scores, issue number, counts, ledger columns |

Self-host `.woff2`, `font-display: swap`. Fallbacks: `Newsreader → Georgia, serif` · `Archivo → system-ui, sans-serif` · `Commit Mono → "IBM Plex Mono", ui-monospace, monospace`.

**On the brand judge's note** ("Newsreader + IBM Plex is competent and free; buy a display face with an owner"): the note is right and the remedy was wrong. Two directions specified commercial faces and both shipped as their free fallback — one of them *Inter*, the exact default it spent a paragraph refusing. Distinctiveness is bought here instead by (a) swapping the generic sans for **Archivo's width axis** — standing heads at `wdth 112`, 600, uppercase, `0.12em` read as institutional signage, not as a SaaS label — and (b) one drawn SVG wordmark. If a licence budget appears later, Newsreader → Signifier or Portrait Text is one `@font-face` and one variable declaration; no layout changes.

### 4.1 The scale — root 16px

| rem / px | Family & weight | Exact use |
|---|---|---|
| `2.625` / 42 | Newsreader 500, lh 1.13, `-0.012em` | **Find title, desktop.** Max-width 24ch. Never truncated. |
| `2.25` / 36 | Newsreader 500, lh 1.15 | Find title, desktop, **step-down** when it renders 4+ lines at 42. |
| `2.75` / 44 | Commit Mono 600, `tabular-nums`, `-0.02em` | **The money figure.** One per screen. Cents at `0.55em` in `--ink-3`. |
| `1.75` / 28 | Newsreader 500, lh 1.18 | Find title, mobile. Step-down to `1.5`/24 at 5+ lines. |
| `1.3125` / 21 | Newsreader 400, lh 1.42, `--ink` | **The lede** — the pre-enumeration clause of `move`. Desktop and mobile. |
| `1.1875` / 19 | Newsreader 400, lh 1.68, 66ch | Reading view body (`/full`) only. |
| `1.125` / 18 | Newsreader 400, lh 1.62, 64ch, `--ink-2` | Body prose, desktop: rationale, expanded steps. |
| `1.0625` / 17 | Newsreader 400, lh 1.55, `--ink-2` | Step rows (both widths); body prose, mobile. |
| `1.0` / 16 | Commit Mono 500 tabular | Record money column; secondary money. |
| `0.9375` / 15 | Archivo 500 | Buttons, nav links, record row titles, list rows. |
| `0.875` / 14 | Newsreader 400, lh 1.5 | Margin note quotes. **Never smaller — this is a receipt.** |
| `0.8125` / 13 | Commit Mono 500, `0.06em`, caps | Kicker, dates, ledger meta, scores. |
| `0.75` / 12 | Commit Mono 400 tabular | Similarity figures, axis ticks, byline. |
| `0.6875` / 11 | Archivo 600 `wdth 112`, `0.12em`, caps, `--ink-3` | Standing heads, source labels, micro-captions. |

Nothing else exists. If you need a size that is not here, you are solving the wrong problem.

**Italic** is reserved for the analyst's own guardrail sentences in "What I won't do without you". Nowhere else.

---

## 5. SPACE, RADIUS, BORDER, SHADOW

```css
/* 4px base. Use the token, never a raw px. */
--s1:4px  --s2:8px  --s3:12px --s4:16px --s5:20px --s6:24px
--s7:32px --s8:40px --s9:48px --s10:64px --s11:80px --s12:96px

/* radii — three values, no others */
--r-sheet:12px    /* the paper sheet on the desk */
--r-block:8px     /* inset blocks: the ask bar, the guardrail block, buttons */
--r-chip:4px      /* the waiting-diamond frame, the 2/6 counter */

/* borders */
--b-paper: 1px solid var(--rule);
--b-paper-strong: 1px solid var(--rule-strong);
--b-desk: 1px solid var(--desk-rule);

/* shadow — exactly two, both only under a sheet on the desk */
--sh-sheet: 0 1px 2px rgba(0,0,0,.30), 0 12px 32px rgba(0,0,0,.28);
--sh-sheet-mobile: 0 1px 2px rgba(0,0,0,.30), 0 6px 16px rgba(0,0,0,.24);
```

No shadow on anything inside a sheet. No hover lift. No blur. Divisions inside a sheet are 1px rules and nothing else. Blocks inside a sheet are `--paper-dim` with `--b-paper`.

Vertical rhythm inside the sheet: `--s6` (24) between blocks, `--s4` (16) between rows inside a block, `--s8` (40) above a standing head.

---

## 6. LAYOUT CHASSIS

### 6.1 Desktop — the column math is exact, there is no stranded column

```
1440 =  56 gutter │ 272 rail │ 40 │ 680 sheet │ 40 │ 296 margin │ 56 gutter
```

The desk colour bleeds edge to edge, so there is never a white void. **The reading column is 680px because 680px is correct for 18px Newsreader** — stretching it would wreck the one thing that matters. The margins are not empty; they carry the two things she actually asks for: **the record on the left** (should I believe this) and **the receipts on the right** (prove it).

**Growth beyond 1440:** the chassis caps at 1560 (`gutter 60 │ rail 300 │ 40 │ sheet 720 │ 40 │ margin 340 │ gutter 60`) and centres. Past that, desk fills. At 2560px the widest bare margin is desk, not paper — it reads as a desk, which is what the masthead band already established.

**Breakpoints:**

| Width | Layout |
|---|---|
| ≥ 1280 | Three columns as above. Rail `position:sticky`. Margin sticky + scroll-linked. |
| 1024–1279 | Rail 240. Margin → 240. Sheet takes the remainder, min 600. |
| 768–1023 | Rail becomes a 64px horizontal strip under the masthead. Margin collapses to an inline pull-tab per sheet. Sheet max 720, centred in paper. |
| < 768 | §6.2. |

### 6.2 Mobile 375

Masthead 52px sticky dark. Sheets on `--desk` with 16px side gutters and 12px between sheets. Paper is ≥88% of the content area.

The decision buttons render **inline, above the fold**. A sticky bottom bar (`72px`, desk, containing only the primary + secondary) appears **only** once the inline buttons scroll out of view, and disappears when they return. It does not exist on first paint, so it does not eat 72px of a 667px screen before she has read anything.

All targets ≥ 44px. Primary decision button 56px. No hover-only affordance anywhere. No bottom sheets that trap scroll — evidence and full text are **routes**, dismissed by the system back gesture.

---

## 7. THE CRUX — TWELVE LINES OF AGENT PROSE

This is the section to get right. Everything else is scaffolding.

### 7.0 What the data actually is

I measured every `move` in the fixture. There are **four shapes, not one**, and any parser that assumes the (1)(2)(3) case silently destroys 6 of the 17 long-form fields:

| Shape | Count | Chars | Example |
|---|---|---|---|
| Enumerated `(1)(2)(3)` | 4 | 813–1097 | `33d26b20`, `8629ea6d` |
| Enumerated `(a)(b)(c)` | 1 | 1054 | `00846dff` |
| Prose, semicolon-structured, **no enumerators** | 2 | 808, 884 | `32745fcf`, `5f1298b8` |
| **One sentence** | 10 | 53–126 | `308f828e`: *"Raise tiramisu from $7 to $9 and hold everything else."* |

Titles run **13 → 98 chars**. Rationales run **203 → 1242 chars**. A design that assumes any single shape breaks on Tuesday.

### 7.1 The stack, in render order

Nothing here is a summary and nothing is destroyed. The full `move` string is present in the DOM on every screen it appears on; every clamp is CSS (`-webkit-line-clamp`), so Ctrl-F finds it, a screen reader reads it, and copy-paste gets all of it.

```
1  KICKER        No. 57 · From 4 reviews and 4 local trends
2  TITLE         whole, never cut
3  THE ASK       money · when it gets checked · [buttons] · "Nothing runs until you say yes."
4  THE LEDE      the pre-enumeration clause of `move`, promoted to 21px
5  THE WORK      "Four things I'll draft. None of them are yours to do."
                 + numbered rows, 2-line CSS clamp, expand in place
6  THE GUARDS    "What I won't do without you" — his own clauses, verbatim, italic
7  THE WHY       rationale, 3-line clamp
8  THE PROOF     "8 memories, strongest first  →"
9  AS WRITTEN    "Read it as written · 214 words  →"
10 BYLINE        "Radar, Analyst and Meter ran at 4:12am."
```

**The 90-second contract:** kicker (1 line) + title (2–3) + ask (3) + lede (1) + work header (1) = **eight to nine lines, ~40 words**, and the decision buttons are element 3, above all of it. She can act without ever scrolling into a paragraph. Elements 5–10 are the thirty extra seconds she may choose to spend.

### 7.2 Element 3 — THE ASK

An inset `--paper-dim` block, `--r-block`, sitting directly under the title.

```
+$25.00 a day          $750 a month if it holds
I'll check it against your sales on 25 Aug.

[ Yes — draft it for me ]  [ Not this one ]     Ask me again next week

Nothing runs until you say yes.
```

- Money figure: `2.75rem` Commit Mono 600, from `predicted_daily_cents`. Per-month is derived and always carries **"if it holds"**. This is the only place the word "predicted" is implied, and it is implied in English.
- `verify_after` becomes *"I'll check it against your sales on 25 Aug."* — the promise that this thing will be graded.
- **"Nothing runs until you say yes."** is set in Archivo 500 `0.9375rem`, `--ink-3`, always present, **in the same pixel position every single morning.** This is the building's voice, not the analyst's — it is a product guarantee, so it is true even when the Analyst wrote nothing about approval. It exists because the owner judge's single sharpest criticism of Ninety Seconds was that this sentence lived two minutes deep in a document.
- `confidence` is **not** rendered as a number. `0.55` is the agent grading its own homework. The evidence count (element 8) is the honest version.

### 7.3 Element 4 — THE LEDE

```ts
const enumStart = move.search(/\((?:1|a)\)/);
const lede = enumStart > 0 ? move.slice(0, enumStart).trim() : firstSentence(move);
```

Rendered at `1.3125rem` Newsreader 400, `--ink`. For `8629ea6d` this yields:

> *I will draft, for your approval before anything goes live:*

That sentence is the single most reassuring thing in the payload and in the rejected render it was character 1 of a grey slab. It gets its own size.

If the lede would exceed 3 rendered lines (as in `33d26b20`, whose lede is 300 chars), clamp it to 2 lines and let the remainder join the guardrail extractor and the full text. It is never lost.

### 7.4 Element 5 — THE WORK ORDER

**The segmenter — a ladder, in this order, first match wins:**

```ts
function segments(move: string): string[] {
  if (move.length < 180) return [];                          // 10 of 17 finds. No list.
  let out =
    splitOn(move, /\((\d)\)\s/g)      ??                     // (1)(2)(3)   — 4 finds
    splitOn(move, /\(([a-e])\)\s/g)   ??                     // (a)(b)(c)   — 1 find
    splitDepth0Semicolons(move)       ??                     // prose       — 2 finds
    splitSentences(move);                                    // last resort — 0 finds today
  return out.slice(0, 7).map(trim);                          // cap 7; remainder merges into row 7
}
```

`splitDepth0Semicolons` ignores semicolons inside `(...)`, `'...'` and `"..."`. Verified against `32745fcf` and `5f1298b8`: both yield 4–6 bounded rows.

**The header — the graft, corrected per §1.4:**

```ts
const yours = segments.filter(isYours).length;
const isYours = (s) => /\byou (review|approve|sign|decide|pick|choose|confirm)\b|\byour (approval|sign-off|OK|yes)\b/i.test(s)
                    && !/^I('ll| will)\b/i.test(s.trim());
```

Rendered in Archivo 500 `0.9375rem`, sentence case, `--ink`:

```
yours = 0 → "Four things I'll draft. None of them are yours to do."
yours = 1 → "Five steps. Four are mine, one is yours."
yours > 1 → "Five steps. Three are mine, two are yours."
```

**The rows:**

```
1   a Saturday-only text-ahead waitlist using the free tier of a
    waitlist tool that works alongside your Square terminal…      ⌄
────────────────────────────────────────────────────────────────────
2   a laminated door card and window QR that says 'Full right now?
    Scan to join the list — we'll text you when your table…'      ⌄
```

- Numeral: Commit Mono 500 `0.8125rem`, `--ink-3`, in a 28px hanging indent.
- Text: Newsreader 400 `1.0625rem`, `--ink-2`, **`-webkit-line-clamp: 2`**.
- Rows separated by `--b-paper`. **No boxes.**
- A row that is `isYours` gets a brass outlined diamond in the hang **and** the word `yours` in `--brass-ink` `0.6875rem` caps at the end of the head line. Never colour alone.
- Chevron `⌄` → `⌃`, rotating 90°. Expansion is `height` + `180ms cubic-bezier(0.2,0,0,1)`, **pushing downward only**, so a tapped row stays under the thumb.

**Explicitly cut: the "bold first clause" head.** I tested it on `8629ea6d`. The first clause boundary in step 1 falls at character 118; in steps 2–4 there is no early boundary at all. A 48-char word-boundary cut produces *"a Saturday-only text-ahead waitlist using the free"* — a sentence that stops mid-thought, which the owner judge named specifically: *"a sentence that stops mid-thought reads as broken, not as clean."* **There is no synthesized head. The numeral is the scannable element and the row clamps at two lines.** Simpler, safer, no heuristic, no fragment.

**When `segments()` returns `[]`** (all 10 short moves): no header, no rows, no chevrons. The move renders as the lede and stops. A layout that only works with three bullets is a layout that breaks on Tuesday.

### 7.5 Element 6 — WHAT I WON'T DO WITHOUT YOU

Standing head, then **his own sentences, verbatim, italic Newsreader `1.0625rem`, `--ink-2`**, on `--paper-dim` with a 2px `--brass-ink` left rule.

The owner judge praised this heading by name (*"the exact thing I scan for, and it has a name and a fixed position"*) and separately rejected ASSAY's synthesized `≤ $50/MO CAP` chips as machine dialect. So: **extract, never rewrite.**

```ts
const CLAUSE = /(?<=[.;])\s+|\s+—\s+/;
const GUARD = [
  /\$\s?\d+\s?(\/mo|per month|month)?\s*cap|under (your|the) \$\d+|zero (new )?spend|no spend/i,  // money
  /\byou (review|approve|sign|OK)\b|\byour (approval|sign-off|OK|yes)\b|before (any|anything|anyone)\b/i, // gate
  /\bnothing (goes live|sends|is repriced|changes)\b|\bno (price|menu) changes\b/i,               // scope
];
guards = dedupe(move.split(CLAUSE).filter(c => GUARD.some(r => r.test(c))))
          .sort(byPatternPriority)      // money, then gate, then scope
          .slice(0, 3);
```

Verified output for `8629ea6d` — three real clauses, unedited:

> *I will confirm the exact tool and any cost with you before signing up, and will not exceed the $50/month cap*
> *You review and OK every piece before it is used.*
> *Nothing gets sent to any customer without your sign-off.*

`guards.length === 0` (all 10 short moves): the block does not render. The permanent line in the ask bar (§7.2) is still there, because it always is. **The clause is not stripped from its step row** — the step clamps at 2 lines and the guardrail clause sits below the clamp anyway; stripping risks mangling his sentence, and duplication in the expanded view is honest.

### 7.6 Element 7 — THE WHY

Standing head `WHY HE THINKS SO`. `rationale` at `1.125rem` Newsreader `--ink-2`, **3-line clamp**, affordance `Three more lines ⌄` — the exact count, computed from `scrollHeight`, never a vague "read more". Expanded, it runs to full length at 64ch. Open by default on desktop, closed on mobile.

Where a literal `[0-9a-f]{8}` in the rationale matches a real `observation_id`, it is replaced by a superscript Commit Mono `0.6875rem` numeral raised `0.42em`, which highlights the matching margin note. **3 of 17 rationales.** Elsewhere: nothing (§1.5).

### 7.7 Elements 8–10

```
8   8 memories, strongest first                                      →
9   Read it as written · 214 words                                   →
10  Radar, Analyst and Meter ran at 4:12am.
```

- The word count on 9 is the proof that nothing was cut. Computed from `move.split(/\s+/).length + rationale.split(/\s+/).length`.
- `/full` is a **route**, not a modal: 66ch, `1.1875rem`/1.68, two standing heads (`WHY HE THINKS SO` / `WHAT HE'LL DO`), both fields verbatim and unparsed, with the proof margin still attached. Clicking a work-order row opens `/full` scrolled to that sentence with `--wash-cited` under it. Header line: **"The long version. About two minutes."**
- Byline derived from `runs[]`: earliest `started_at` of the most recent night.

### 7.8 Failure behaviour

Worst realistic case — 1,100 characters, no enumerators, no semicolons, one run-on: `splitSentences` still yields 5–7 bounded rows; the guardrail extractor still fires on `$50` and `approval`; the ask bar and its permanent line are unaffected. **The floor is "bounded rows of prose plus a verbatim view." It is never the unbroken grey wall that got rejected.** And per §1.8, none of that is ever announced on screen.

---

## 8. THE SIGNATURE — THE PROOF MARGIN

The right-hand 296px column of every find. It is the only reason the 1440px viewport is honest, and it is the only element that discharges "we show the receipts" as a permanent structure rather than a link.

### 8.1 Anatomy of one note

```
1   REVIEW · 19 JUL                                     0.31
    ██████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
    "Turned away on Saturday at 8pm. They said an
     hour and a half. We ate at Lucca's instead,
     which was fine but not as good."
```

| Part | Spec |
|---|---|
| Index | Commit Mono 500 `0.8125rem`, `--ink-3`, 20px hanging indent |
| Label | Archivo 600 `wdth 112` `0.6875rem` caps `0.12em`, `--ink-3`. `kindLabel(kind)` + ` · ` + `DD MMM` |
| Figure | Commit Mono 400 `0.75rem` `--ink-3`, right-aligned. **Desktop only** — absent on mobile margin cards, present on `/evidence`. |
| **Weight rule** | 2px, `--ink-2`, `width: max(6px, similarity / 0.75 × trackWidth)`. Track = note width. No floor line, no threshold tick, no colour change at any value. |
| Quote | Newsreader 400 `0.875rem`/1.5, **`--ink-2` at 100%, always.** Never faded. Full text, `-webkit-line-clamp: 4`, expands in place. |

**The 0.75 scale is fixed and global**, not normalised per find. Corpus max is 0.702, so the strongest observation the system has ever retrieved fills 94% of its track and a typical one (median 0.308) sits at 41%. A weak find looks weak. That is the point, and it is why the scale must never be per-find.

Order: `similarity` descending (§1.7).

### 8.2 Footer — the plain-language equivalent, always

```
────────────────────────────────────────────
8 memories, strongest first
127 remembered in all                    →
```

This is the Board & Book graft. The mark is a fast path for anyone who notices it; the sentence costs nothing to anyone who never does. **No score, no floor, no threshold, no verdict is ever derived from retrieval on this screen.**

On the record's miss row, the derived comparison line from §1.2 appears instead:

```
He wrote this from 2 memories.
Every other find here stood on at least 4.
```

### 8.3 Behaviour

- **Scroll-linked, sticky.** Notes track the block that cites them. Notes never reorder or reflow while she is reading — opacity is the only thing that changes.
- **Focus:** hovering or tapping any block carrying superscripts drops uncited notes to 20% opacity over 120ms; back over 90ms. Opacity only.
- **Tap a note** → `/find/:id/evidence` scrolled to it.
- **< 1024px:** the entire margin collapses to one full-width pull-tab at the foot of the sheet — `Proof · 8 memories  →` — opening `/find/:id/evidence` as a route. Zero space cost, one tap.

### 8.4 Motion — the only motion the signature has

On first paint of a find (once per find, keyed in `sessionStorage`, never replayed):

```
weight rules draw left → right
  width 0 → target, 200ms cubic-bezier(0.2, 0, 0, 1)
  stagger 20ms by display rank (strongest first)
  8 notes → 340ms total
```

It is functional, not decorative: the eye is walked down the ranked evidence before a word is read, and the staircase of decreasing lengths is legible as a shape in a third of a second. Nothing else about a note animates. `prefers-reduced-motion: reduce` → rules render at full width immediately.

### 8.5 Why this is not portable

Every sentence this agent writes was produced from retrieved observations with a numeric similarity attached, stored in `find_evidence` alongside the prediction that will later be graded. There is nothing to footnote on a revenue chart. Put the Proof Margin on a generic analytics dashboard and the column is empty.

---

## 9. CHARTS

Two visuals in the entire product, plus the signature. Everything else is a table, because books of record contain tables.

Rules across both: no gridlines, no legends (marks are labelled in place), no gradients, no rounded caps, no tooltips carrying information not also on screen, 2px surface gap between adjacent fills, `tabular-nums` everywhere, and every state encoded three ways — fill, texture, and the spelled word.

### 9.1 THE ROAD — one measure, three regions

Full sheet width, 72px tall (not 44 — the rejected strip was a sliver).

```
$0                                                          $8,000 a month
├──────────────────────────────┬──────┬─────────────────────────────────┤
│▓▓▓▓▓▓ verified ▓▓▓▓▓▓▓▓▓▓▓▓▓▓│//////│                                 │
├──────────────────────────────┴──┬───┴─────────────────────────────────┤
  $3,795 a month                  │      $3,335 a month
  earning now, checked            │      still to find
  against your sales              │
                                  └─ $870 a month · 2 waiting on you  ⌄
```

- **Earned** `#118066` solid, `earnedMo / goalMo`.
- **Waiting on you** `#A8781F` at 45° hatch (3px on / 5px off), `waitingMo / goalMo`. Hatch is why 10.9% stays loud beside 47%; a hatched region reads at any width where a flat fill does not.
- **Still to find** — no fill. `--rule-strong` outline, `--paper` interior, ticks every $1,000.
- 2px `--paper` gap between regions.
- The brass region carries **the only leader line and the only caret on the page**, and is **the only clickable region of the bar** → `/road`. The most actionable 11% is the loudest object.
- Min width for the brass region: **48px**. Below that it renders at 48px with a `⤺` mark on the boundary and the label carries the true figure. Never zero-width, never mislabelled.
- **Overflow rule:** if `earnedMo + waitingMo > goalMo`, the bar rescales to that sum and the goal becomes a 2px `--ink` vertical tick with the label `$8,000 goal` set on it. Never clip, never let a region silently vanish. (This will happen: with different data brass is routinely larger than green.)

**What is deliberately NOT on the bar**, in words underneath — because drawing unverified money on a bar labelled "earning" is exactly the projection this product exists to refuse:

```
NOT ON THIS BAR
4 accepted, being drafted           $2,340 a month   if they hold
4 measured, not yet counted         $3,918 a month   estimated, not verified

Neither is money until the Meter checks it against your sales.
```

### 9.2 THE TWO-POINT PROBLEM — refused, and the refusal is the design

`monthly` has exactly two rows. The rejected render plotted them: two bars, an enormous void, a goal line floating overhead. That reads as a broken data pipeline, and no amount of styling fixes a granularity error.

**There is no growth chart.** In its place, on `/road`:

```
IS IT GROWING?

Two months of ledger. A line needs six.                            2 / 6

June          added  $66.00 a day
July          added  $60.50 a day
                     ──────────────
                     $126.50 a day    earning now

We'll draw the line in October. Until then, two numbers are two numbers.
```

- Two rows, right-aligned Commit Mono 500 `1rem` tabular, a 1px `--rule-strong` above the total. **At n=2 a table is complete and a chart is not.** The table hides nothing; the chart implied missing data.
- `2 / 6` sits in a `--r-chip` outline box, Commit Mono 500 `0.8125rem`, `--ink-3`. It is a promise with a date attached.
- At `monthly.length >= 6` the block is replaced by a 2px `#118066` step line (square joins — money arrives in discrete verified steps, not a smooth curve) with the goal as the plot's **top frame rule**, labelled on the frame, never floating. Ship the `<Growth>` component with both branches now; the switch is `monthly.length >= 6`.

This is the strongest thing in the product and it costs two DOM nodes. A product that publishes its miss refusing to draw a trend through two points is the same behaviour twice.

### 9.3 THE RECORD SPINE — a table, not a chart

Eleven filed entries at n=11 are denser, faster and more credible as a typeset list than as any chart — and eleven rows genuinely fill a column, where two bars genuinely look broken. Full spec in §10.2.

---

## 10. SCREENS

Five routes. `/` is the default landing every morning; there is no home screen to pass through. Every route is URL-addressable so the nightly push opens directly onto the decision.

```
/                     This morning — one find
/record               The record — 11 filed entries
/road                 The road — the measure, the refusal, what's waiting
/find/:id/evidence    Evidence — the full proof margin as a screen
/find/:id/full        Read it as written — verbatim
/memory               Search 127 observations   (reached from the margin footer and ⌘K)
```

Navigation is a masthead line. **No tabs** — tabs were the second rejection and they gave four things equal weight when the product has one job per morning. Current route carries a 2px underline; brass only if that section contains something awaiting her.

### 10.1 THIS MORNING — 1440

```
┌─ 1440 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│▓▓▓▓▓▓▓▓ desk #11151C · 60px · sticky ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓│
│      BRASS TACKS   No. 57 · Tue 28 Jul        This morning   The record   The road            ◈ 2 waiting on you            ⌕ 127 remembered         │
├──────┬────────────────────────────┬────┬───────────────────────────────────────────────────────────────────────┬────┬───────────────────────────┬────┤
│ 56   │  272  rail #181D26         │ 40 │  680  sheet #F7F5EF  r12  --sh-sheet                                  │ 40 │  296  PROOF MARGIN        │ 56 │
│      │                            │    │                                                                       │    │                           │    │
│      │  THE RECORD                │    │  No. 57 · FROM 4 REVIEWS AND 4 LOCAL TRENDS                            │    │  WHAT HE READ             │    │
│      │  6 of 7 calls right        │    │                                                                       │    │  ───────────────────────  │    │
│      │  ────────────────────────  │    │  Stop the Saturday walkaway:                                          │    │  1  TREND · 27 JUN   0.51 │    │
│      │  ● 6  verified             │    │  a text-ahead waitlist you                                            │    │     ████████████████████  │    │
│      │  ⊘ 1  miss, published      │    │  don't have to answer                                                 │    │     "Local diners increas-│    │
│      │  ◐ 4  measuring            │    │  the phone for                                                        │    │      ingly cite wait time │    │
│      │  ────────────────────────  │    │                            ← 42px Newsreader 500, 24ch, never cut     │    │      as the reason for    │    │
│      │  EARNING NOW               │    │  ┌─ THE ASK ── #EFEDE6 r8 ──────────────────────────────────────────┐ │    │      abandoning a restau- │    │
│      │  $126.50                   │    │  │                                                                 │ │    │      rant choice on week- │    │
│      │  a day, checked against    │    │  │  +$25.00 a day        $750 a month if it holds                   │ │    │      ends."               │    │
│      │  your real sales           │    │  │  I'll check it against your sales on 25 Aug.                     │ │    │  ───────────────────────  │    │
│      │  ────────────────────────  │    │  │                                                                 │ │    │  2  REVIEW · 12 JUN  0.39 │    │
│      │  THE ROAD                  │    │  │  [ Yes — draft it for me ] [ Not this one ]  Ask me again next   │ │    │     ███████████████       │    │
│      │  ▓▓▓▓▓▓▓▓▓▓▓▓▓////░░░░░░░  │    │  │                                              week               │ │    │     "Great food, terrible │    │
│      │  47% earned · 11% waiting  │    │  │                                                                 │ │    │      wait management. You │    │
│      │  ────────────────────────  │    │  │  Nothing runs until you say yes.                                │ │    │      stand in a doorway   │    │
│      │  ⌕ search 127 observations │    │  └─────────────────────────────────────────────────────────────────┘ │    │      blocking servers…"   │    │
│      │                            │    │                                                                       │    │  ───────────────────────  │    │
│      │                            │    │  I will draft, for your approval before anything                      │    │  3  REVIEW · 17 JUL  0.36 │    │
│      │                            │    │  goes live:                          ← 21px Newsreader, --ink         │    │     ██████████████        │    │
│      │                            │    │                                                                       │    │     "Weekend evening din- │    │
│      │                            │    │  Four things I'll draft. None of them are yours to do.                │    │      ing demand in the    │    │
│      │                            │    │  ──────────────────────────────────────────────────────────────────   │    │      district peaks shar- │    │
│      │                            │    │  1   a Saturday-only text-ahead waitlist using the free tier of a     │    │      ply between 7pm and  │    │
│      │                            │    │      waitlist tool that works alongside your Square terminal…    ⌄    │    │      8:30pm."             │    │
│      │                            │    │  ──────────────────────────────────────────────────────────────────   │    │  ───────────────────────  │    │
│      │                            │    │  2   a laminated door card and window QR that says 'Full right        │    │  4  REVIEW · 19 JUN  0.34 │    │
│      │                            │    │      now? Scan to join the list — we'll text you when your…'     ⌄    │    │     █████████████         │    │
│      │                            │    │  ──────────────────────────────────────────────────────────────────   │    │     "Second Saturday in a │    │
│      │                            │    │  3   a one-page host script with honest quote bands (e.g. '35–50      │    │      row we could not get │    │
│      │                            │    │      minutes, we'll text you at 20 minutes out') plus a rule…    ⌄    │    │      a table…"            │    │
│      │                            │    │  ──────────────────────────────────────────────────────────────────   │    │  ───────────────────────  │    │
│      │                            │    │  4   a two-line SMS template for the ready ping and a 'still          │    │  5  REVIEW · 05 JUN  0.33 │    │
│      │                            │    │      coming?' nudge.                                             ⌄    │    │  ───────────────────────  │    │
│      │                            │    │                                                                       │    │  6  REVIEW · 19 JUL  0.31 │    │
│      │                            │    │  WHAT I WON'T DO WITHOUT YOU                                          │    │  ───────────────────────  │    │
│      │                            │    │  ┃ I will confirm the exact tool and any cost with you before         │    │  7  TREND · 03 JUL   0.30 │    │
│      │                            │    │  ┃ signing up, and will not exceed the $50/month cap                  │    │  ───────────────────────  │    │
│      │                            │    │  ┃ You review and OK every piece before it is used.                   │    │  8  TREND · 09 JUN   0.26 │    │
│      │                            │    │  ┃ Nothing gets sent to any customer without your sign-off.           │    │  ───────────────────────  │    │
│      │                            │    │           ↑ his sentences, verbatim, italic, 2px brass left rule      │    │  8 memories,              │    │
│      │                            │    │                                                                       │    │  strongest first          │    │
│      │                            │    │  WHY HE THINKS SO                                                     │    │  127 remembered in all  → │    │
│      │                            │    │  Saturday is the only night with a real wait, and the reviews show    │    │                           │    │
│      │                            │    │  the wait is not just long, it is unmanaged: people stand in the      │    │                           │    │
│      │                            │    │  doorway blocking servers, get no straight answer on timing, and…     │    │                           │    │
│      │                            │    │  Six more lines ⌄                                                     │    │                           │    │
│      │                            │    │                                                                       │    │                           │    │
│      │                            │    │  8 memories, strongest first                                      →   │    │                           │    │
│      │                            │    │  Read it as written · 214 words                                   →   │    │                           │    │
│      │                            │    │                                                                       │    │                           │    │
│      │                            │    │  Radar, Analyst and Meter ran at 4:12am.        ← 12px mono --ink-3    │    │                           │    │
│      │                            │    │                                                                       │    │                           │    │
│      │                            │    ├───────────────────────────────────────────────────────────────────────┤    │                           │    │
│      │                            │    │  ALSO WAITING ON YOU · 1 more · $120 a month                          │    │                           │    │
│      │                            │    │  ◈  Validated parking at the nearby garage        $12.00 a day    →   │    │                           │    │
│      │                            │    │     5 memories · you set this aside on 14 Jul                         │    │                           │    │
│      └────────────────────────────┴────┴───────────────────────────────────────────────────────────────────────┴────┴───────────────────────────┴────┘
```

### 10.2 THIS MORNING — 375

```
┌─ 375 ───────────────────────────────────┐
│▓ BRASS TACKS  No. 57 · Tue 28 Jul   ⌕ ▓│ 52 sticky desk
│▓ This morning  The record  The road   ▓│
├─────────────────────────────────────────┤
│ ░░ desk 16px gutter ░░                  │
│ ┌─ paper #F7F5EF r12 ─────────────────┐ │
│ │ No. 57 · FROM 4 REVIEWS AND         │ │ 11px Archivo caps
│ │ 4 LOCAL TRENDS                      │ │
│ │                                     │ │
│ │ Stop the Saturday                   │ │ 28px Newsreader 500
│ │ walkaway: a text-ahead              │ │ 5 lines — NOT cut,
│ │ waitlist you don't have             │ │ steps to 24px at 5+
│ │ to answer the phone for             │ │
│ │                                     │ │
│ │ ┌─ #EFEDE6 r8 ─────────────────────┐│ │
│ │ │ +$25.00 a day                    ││ │ 44px Commit Mono 600
│ │ │ $750 a month if it holds         ││ │ 13px mono --ink-3
│ │ │ I'll check it against your       ││ │ 15px Archivo
│ │ │ sales on 25 Aug.                 ││ │
│ │ │                                  ││ │
│ │ │ [  Yes — draft it for me   ]     ││ │ 56px, ink fill
│ │ │ [      Not this one        ]     ││ │ 48px, ink outline
│ │ │       Ask me again next week     ││ │ 44px, text only
│ │ │                                  ││ │
│ │ │ Nothing runs until you say yes.  ││ │ ALWAYS. SAME PLACE.
│ │ └──────────────────────────────────┘│ │
│ │                                     │ │  ← fold at 812
│ │ I will draft, for your approval     │ │ 21px Newsreader
│ │ before anything goes live:          │ │
│ │                                     │ │
│ │ Four things I'll draft. None of     │ │ 15px Archivo 500
│ │ them are yours to do.               │ │
│ │ ─────────────────────────────────── │ │
│ │ 1  a Saturday-only text-ahead       │ │ 17px Newsreader
│ │    waitlist using the free…      ⌄  │ │ 2-line CSS clamp
│ │ ─────────────────────────────────── │ │
│ │ 2  a laminated door card and     ⌄  │ │
│ │    window QR that says 'Full…'      │ │
│ │ ─────────────────────────────────── │ │
│ │ 3  a one-page host script with   ⌄  │ │
│ │    honest quote bands (e.g.…)       │ │
│ │ ─────────────────────────────────── │ │
│ │ 4  a two-line SMS template for   ⌄  │ │
│ │    the ready ping and a 'still…'    │ │
│ │                                     │ │
│ │ WHAT I WON'T DO WITHOUT YOU         │ │
│ │ ┃ I will confirm the exact tool     │ │ italic Newsreader
│ │ ┃ and any cost with you before      │ │ 2px brass left rule
│ │ ┃ signing up, and will not exceed   │ │
│ │ ┃ the $50/month cap                 │ │
│ │ ┃ You review and OK every piece     │ │
│ │ ┃ before it is used.                │ │
│ │ ┃ Nothing gets sent to any          │ │
│ │ ┃ customer without your sign-off.   │ │
│ │                                     │ │
│ │ ┌─────────────────────────────────┐ │ │
│ │ │ PROOF · 8 memories            → │ │ │ 56px pull-tab
│ │ └─────────────────────────────────┘ │ │ → /find/:id/evidence
│ │                                     │ │
│ │ WHY HE THINKS SO                    │ │
│ │ Saturday is the only night with a   │ │ 17px, 3-line clamp
│ │ real wait, and the reviews show…    │ │
│ │ Six more lines ⌄                    │ │
│ │                                     │ │
│ │ Read it as written · 214 words   →  │ │
│ │ Radar, Analyst and Meter ran 4:12am │ │
│ └─────────────────────────────────────┘ │
│ ░░ 12px ░░                              │
│ ┌─────────────────────────────────────┐ │
│ │ ALSO WAITING · 1 more · $120/month  │ │
│ │ ◈ Validated parking at the       →  │ │
│ │   nearby garage      $12.00 a day   │ │
│ └─────────────────────────────────────┘ │
│ ░░ 12px ░░                              │
│ ┌─────────────────────────────────────┐ │
│ │ THE RECORD · 6 of 7 calls right     │ │
│ │ ● 6 verified  ⊘ 1 miss  ◐ 4 measur. │ │
│ │ $126.50 a day, checked against      │ │
│ │ your real sales                     │ │
│ │ Open the record                  →  │ │
│ └─────────────────────────────────────┘ │
├─────────────────────────────────────────┤
│▓ [ Yes — draft it ]  [ Not this one ] ▓│ 72px sticky, desk
└─────────────────────────────────────────┘  ONLY after inline
                                              buttons scroll away
```

### 10.3 THE RECORD

Judged and measuring only. **No waiting-on-you rows** — that is the §3.4 constraint that keeps the palette legal, and it is also correct: the record is what happened.

```
1440 — sheet 680, rail and margin as §10.1. Margin shows the selected row's evidence.
┌───────────────────────────────────────────────────────────────────────┐
│  THE RECORD                                                           │
│  6 of 7 calls right · 11 filed · 1 miss, published                    │
│  $126.50 a day, checked against your real sales                       │
│                                                                       │
│  ─────────────────────────────────────────────────────────────────    │
│      FILED    WHAT IT WAS              HE SAID    IT MADE      HOW    │
│  ─────────────────────────────────────────────────────────────────    │
│  ●   14 JUL   Raise tiramisu to $9      $23.00    $25.00    9 mem  →  │
│      Verified · 233 tiramisu sold at the new price with no drop        │
│      in dessert attach rate.                                          │
│  ─────────────────────────────────────────────────────────────────    │
│  ●   09 JUL   Saturday text-ahead       $38.00    $41.00    9 mem  →  │
│      Verified · 41 parties seated from the waitlist over two weeks     │
│      that would previously have been standing at the door.            │
│  ─────────────────────────────────────────────────────────────────    │
│  ⊘   02 JUL   Espresso upsell after     $12.00    $̶0̶.̶0̶0̶     2 mem  →  │
│      dessert                                                          │
│      Miss · No measurable lift. Espresso interest locally is flat      │
│      and the evidence for this was thin — two weakly related           │
│      observations. Pulled after four days.                            │
│      He wrote this from 2 memories. Every other find here stood        │
│      on at least 4.                                                   │
│  ─────────────────────────────────────────────────────────────────    │
│  ●   28 JUN   Patio on warm nights      $16.00    $19.00    7 mem  →  │
│  ─────────────────────────────────────────────────────────────────    │
│  ◐   24 JUN   Set menu, Tue–Thu         $20.00       —      8 mem  →  │
│      Measuring · modelled from the prediction; no sales data           │
│      connected. Not counted as a win.                                 │
│  ─────────────────────────────────────────────────────────────────    │
│  … 6 more                                                             │
└───────────────────────────────────────────────────────────────────────┘

375 — same rows, two lines each, money right-aligned:
┌─────────────────────────────────────────┐
│ THE RECORD                              │
│ 6 of 7 calls right · 11 filed           │
│ 1 miss, published                       │
│ ─────────────────────────────────────── │
│ ● 14 JUL  Raise tiramisu to $9          │
│   Verified          said $23 · got $25  │
│ ─────────────────────────────────────── │
│ ⊘ 02 JUL  Espresso upsell after dessert │
│   Miss              said $12 · got $̶0̶   │
│   No measurable lift. Espresso interest │
│   locally is flat and the evidence for  │
│   this was thin — two weakly related    │
│   observations. Pulled after four days. │
│   He wrote this from 2 memories. Every  │
│   other find here stood on at least 4.  │
│ ─────────────────────────────────────── │
│ ◐ 24 JUN  Set menu, Tue–Thu             │
│   Measuring         said $20 · —        │
└─────────────────────────────────────────┘
```

- Ordered by `measured_at` descending. **The miss sits in date order at full weight** — never demoted, never dimmed, never behind a filter. It is the most persuasive row on the page.
- `note` renders verbatim, `1.0625rem` Newsreader `--ink-2`. It already reads as a post-mortem; do not rewrite it.
- `$0.00` on the miss row is struck through, and the strike is the primary encoding — legible in greyscale, in a photograph, and at arm's length through flour.
- Marks: `●` filled disc / `⊘` open square with diagonal strike / `◐` ring closed by a dotted arc. Inline SVG, 12px. **No emoji anywhere in this product, ever.**
- `9 mem` links to `/find/:id/evidence`.

### 10.4 THE ROAD

```
1440 — sheet 680
┌───────────────────────────────────────────────────────────────────────┐
│  THE ROAD TO $8,000 A MONTH                                           │
│                                                                       │
│  $0                                                     $8,000/month  │
│  ├──────────────────────────────┬──────┬───────────────────────────┤  │
│  │▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓│//////│                           │  │ 72px
│  ├──────────────────────────────┴──┬───┴───────────────────────────┤  │
│  ├─────┬─────┬─────┬─────┬─────┬───┼─┬─────┬─────┬─────┬─────┬─────┤  │
│  0    1k    2k    3k    4k    5k   │ 6k    7k    8k                   │
│                                    │                                  │
│  $3,795 a month                    └─ $870 a month                    │
│  earning now, checked                 2 waiting on you            ⌄   │
│  against your sales                                                   │
│                                       $3,335 a month still to find    │
│                                                                       │
│  NOT ON THIS BAR                                                      │
│  4 accepted, being drafted          $2,340 a month   if they hold     │
│  4 measured, not yet counted        $3,918 a month   estimated        │
│  Neither is money until the Meter checks it against your sales.       │
│                                                                       │
│  ─────────────────────────────────────────────────────────────────    │
│  IS IT GROWING?                                                       │
│                                                                       │
│  Two months of ledger. A line needs six.                    [ 2 / 6 ] │
│                                                                       │
│  June                                    added   $66.00 a day         │
│  July                                    added   $60.50 a day         │
│                                                  ─────────────        │
│                                                  $126.50 a day        │
│                                                  earning now          │
│                                                                       │
│  We'll draw the line in October. Until then, two numbers are          │
│  two numbers.                                                         │
│                                                                       │
│  ─────────────────────────────────────────────────────────────────    │
│  WAITING ON YOU · 2 · $870 A MONTH                                    │
│  ◈  Stop the Saturday walkaway            $25.00 a day  8 mem     →   │
│  ◈  Validated parking at the nearby       $12.00 a day  5 mem     →   │
│     garage                                                            │
└───────────────────────────────────────────────────────────────────────┘

375 — bar goes full width, labels stack beneath, table unchanged.
The bar is 64px tall on mobile; the leader line drops from the brass
region to a full-width row, still the only caret on the screen.
```

### 10.5 EVIDENCE

```
1440 — this is the one route where the sheet widens to 976 (sheet + margin),
because the receipts ARE the content and there is no margin to attach.

┌──────────────────────────────────────────────────────────────────────────────────────────┐
│  ← Back to this morning                                                                  │
│                                                                                          │
│  EIGHT MEMORIES BEHIND THIS FIND                                                         │
│  Stop the Saturday walkaway: a text-ahead waitlist you don't have to answer the phone for │
│                                                                                          │
│  Match is how close a memory sits to the idea. Higher is closer.                          │
│  Nothing here passes or fails on it.                                                     │
│  ──────────────────────────────────────────────────────────────────────────────────────  │
│  1   TREND · TRENDS · 27 JUN                                                    0.51     │
│      ████████████████████████████████████████████████████████████████░░░░░░░░░░░░░░░     │
│      "Local diners increasingly cite wait time as the reason for abandoning a            │
│       restaurant choice on weekends."                                                    │
│  ──────────────────────────────────────────────────────────────────────────────────────  │
│  2   REVIEW · REVIEW_SITE · 12 JUN                                              0.39     │
│      ████████████████████████████████████████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░     │
│      "Great food, terrible wait management. You stand in a doorway blocking servers      │
│       because there is nowhere to put people."                                           │
│  ──────────────────────────────────────────────────────────────────────────────────────  │
│  3   TREND · TRENDS · 17 JUL                                                    0.36     │
│      ████████████████████████████████████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░     │
│      "Weekend evening dining demand in the district peaks sharply between 7pm and 8:30pm."│
│  ──────────────────────────────────────────────────────────────────────────────────────  │
│  … 4, 5, 6, 7, 8                                                                         │
│  ──────────────────────────────────────────────────────────────────────────────────────  │
│  8 memories, strongest first. 127 remembered in all.                                     │
│  Search everything he's ever seen                                                   →    │
└──────────────────────────────────────────────────────────────────────────────────────────┘

375 — same list, quote at 17px, weight rule full width, match figure
under the source label rather than right-aligned.
```

### 10.6 THE EMPTY STATE — nothing needs you

**Not one of the five directions specified this**, and it is a common and *desirable* outcome for a nightly product. It is also the screen that proves the thing is not padding its output. Route `/` when `todaysFind === null`.

```
1440
┌──────┬────────────────────────────┬────┬───────────────────────────────────────────────┬────┬───────────────────┬────┐
│ 56   │ rail — unchanged           │ 40 │  sheet 680                                    │ 40 │ 296               │ 56 │
│      │  THE RECORD                │    │  No. 57 · TUE 28 JUL                          │    │  WHAT HE READ     │    │
│      │  6 of 7 calls right        │    │                                               │    │  LAST NIGHT       │    │
│      │  ● 6  ⊘ 1  ◐ 4             │    │  Nothing needs you                            │    │  ───────────────  │    │
│      │  ────────────────────────  │    │  this morning.                                │    │  50 new things.   │    │
│      │  EARNING NOW               │    │                    ← 42px Newsreader 500      │    │  3 kept.          │    │
│      │  $126.50                   │    │                                               │    │  47 discarded as  │    │
│      │  a day, checked against    │    │  Radar read 50 new things overnight.          │    │  noise or already │    │
│      │  your real sales           │    │  None of them are worth your money yet.       │    │  known.           │    │
│      │  ────────────────────────  │    │                    ← 21px Newsreader --ink    │    │  ───────────────  │    │
│      │  THE ROAD                  │    │                                               │    │  ⌕ Search the 127 │    │
│      │  ▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░  │    │  We'd rather show you nothing than pad         │    │    he remembers → │    │
│      │  47% earned · 0 waiting    │    │  the list.        ← 18px Newsreader --ink-2    │    │                   │    │
│      │  ────────────────────────  │    │                                               │    │                   │    │
│      │  ⌕ search 127 observations │    │  ───────────────────────────────────────────  │    │                   │    │
│      │                            │    │  STILL RUNNING                                │    │                   │    │
│      │                            │    │  ◐  Set menu, Tue–Thu       measuring, 12 of   │    │                   │    │
│      │                            │    │                             30 days        →  │    │                   │    │
│      │                            │    │  ◐  Lunch prix-fixe         measuring, 4 of    │    │                   │    │
│      │                            │    │                             30 days        →  │    │                   │    │
│      │                            │    │  ───────────────────────────────────────────  │    │                   │    │
│      │                            │    │  $126.50 a day is still earning while you      │    │                   │    │
│      │                            │    │  read this.                                →  │    │                   │    │
│      │                            │    │                                               │    │                   │    │
│      │                            │    │  Radar, Analyst and Meter ran at 4:12am.      │    │                   │    │
└──────┴────────────────────────────┴────┴───────────────────────────────────────────────┴────┴───────────────────┴────┘

375
┌─────────────────────────────────────────┐
│▓ BRASS TACKS  No. 57 · Tue 28 Jul   ⌕ ▓│
│▓ This morning  The record  The road   ▓│   ← NO brass line: nothing is waiting
├─────────────────────────────────────────┤
│ ┌─────────────────────────────────────┐ │
│ │ No. 57 · TUE 28 JUL                 │ │
│ │                                     │ │
│ │ Nothing needs you                   │ │ 28px Newsreader 500
│ │ this morning.                       │ │
│ │                                     │ │
│ │ Radar read 50 new things            │ │ 21px
│ │ overnight. None of them are         │ │
│ │ worth your money yet.               │ │
│ │                                     │ │
│ │ We'd rather show you nothing        │ │ 17px --ink-2
│ │ than pad the list.                  │ │
│ │                                     │ │
│ │ ─────────────────────────────────── │ │
│ │ STILL RUNNING                       │ │
│ │ ◐ Set menu, Tue–Thu    12 of 30  →  │ │
│ │ ◐ Lunch prix-fixe       4 of 30  →  │ │
│ │ ─────────────────────────────────── │ │
│ │ $126.50 a day is still earning   →  │ │
│ │ while you read this.                │ │
│ │ Radar, Analyst and Meter ran 4:12am │ │
│ └─────────────────────────────────────┘ │
│ ░░ 12px ░░                              │
│ ┌─────────────────────────────────────┐ │
│ │ THE RECORD · 6 of 7 calls right  →  │ │
│ └─────────────────────────────────────┘ │
└─────────────────────────────────────────┘
```

**No sticky bottom bar** on the empty state — there is nothing to decide, so there is no bar. The masthead's brass line is absent. The one warm thing on the page is gone, and its absence is itself the answer to question one.

The "50 new things / 3 kept" figures come from `runs[].note` on the Radar row. If the note is unparseable, the margin block does not render and the sheet stands alone. Never invent the count.

---

## 11. COMPONENT INVENTORY

Every component ships all five states. A component without its empty and error states is not done.

| Component | Loading | Empty | Error | Long text | No pending decision |
|---|---|---|---|---|---|
| `<Masthead>` | Issue no. and date render immediately (client clock + corpus.earliest); nav is inert | — | Nav renders; brass line hidden | Business name truncates with CSS at 24ch; issue no. never truncates | Brass "N waiting on you" line is **removed**, not zeroed |
| `<Rail>` | Three ruled feint lines, no shimmer, caption *"Reading last night's work…"* | Never empty — `summary` always exists | Rail hidden entirely rather than showing wrong money | `business.name` clamps 2 lines | Road mini-bar shows 0% brass; caption reads `nothing waiting` |
| `<FindSheet>` | Feint-ruled skeleton at real heights (title 3 lines, ask block 96px, 4 step rows) | → `<EmptyMorning>` | → `<SheetError>` | This is the long-text component. §7. | Not rendered |
| `<TheAsk>` | Money figure `—` in `--ink-3`; buttons disabled, label unchanged | — | Buttons disabled + inline `Couldn't reach the ledger. Your record is safe.` | Money is bounded by `MAX_PREDICTED_DAILY_CENTS`; 7 digits max at 44px in 680px | Not rendered |
| `<WorkOrder>` | 4 feint rows | `segments.length === 0` → **component absent**, no header, no rows (10 of 17 finds) | Falls back to full `move` at reading measure | Rows clamp 2 lines; expand pushes down; `> 7` merges into row 7 | Not rendered |
| `<Guards>` | absent | `guards.length === 0` → **component absent** | absent | `> 3` clauses → first 3; rest in `/full` | Not rendered |
| `<ProofMargin>` | Note frames with 6px weight rules, quotes as feint lines | `evidence.length === 0` → *"He wrote this from nothing in memory."* + the note in `--miss-ink`. **Should never happen; make it loud if it does.** | *"Couldn't load the receipts."* + retry | Quote clamps 4 lines, expands in place | Shows last night's Radar summary (§10.6) |
| `<TheRoad>` | Bar outline only, no fills, ticks present | `goalMo === 0` → bar replaced by *"No goal set yet."* + link | Bar hidden; the two money figures still render from `summary` | Region labels move below the bar under 96px; brass min-width 48px | Brass region absent; label reads `nothing waiting on you` |
| `<Growth>` | Two feint rows | `monthly.length === 0` → *"The ledger opens with your first verified find."* | Hidden | — | Unaffected |
| `<RecordList>` | 5 feint rows | `judged.length === 0` → *"Nothing has been judged yet. The first check lands on 25 Aug."* | *"Couldn't load the record."* + retry | `note` clamps 3 lines; title clamps 2 | Unaffected — this is the screen that still works |
| `<EvidenceScreen>` | Note frames | Same as `<ProofMargin>` | Retry | Quotes render in full, no clamp | Unaffected |
| `<ReadingView>` | Feint paragraph rules | `move === null` → renders `rationale` alone under one head | Retry | This is the unclamped surface. No max. | Unaffected |
| `<Verdict>` | — | — | — | — | Mark + word, always both. Never renders from colour alone. |
| `<DecisionBar>` (mobile sticky) | Not rendered | Not rendered | Not rendered | — | **Not rendered** |

**Loading, globally:** ruled feint lines at the real heights of the content that is coming, in `--rule`, with one caption in Archivo `0.6875rem` caps: `READING LAST NIGHT'S WORK`. **No shimmer, no pulse, no spinner.** A skeleton that pulses is a loading state performing.

---

## 12. COPY DECK

Every string. Sentence case except standing heads, which are Archivo caps. Never a word Rosa would not say out loud.

**Masthead**
```
BRASS TACKS                                   (drawn SVG wordmark)
No. 57 · Tue 28 Jul
This morning   The record   The road
◈ 2 waiting on you                            (brass; hidden at 0)
⌕ 127 remembered
```

**The ask**
```
+$25.00 a day
$750 a month if it holds
I'll check it against your sales on 25 Aug.
Yes — draft it for me                         (primary)
Not this one                                  (secondary)
Ask me again next week                        (tertiary, text)
Nothing runs until you say yes.               (permanent, never conditional)
```

**After "Yes"** — replaces the button row in place, no dialog, no modal:
```
Drafting. Nothing has gone live.
You'll see every piece before anyone else does.
I'll check whether it paid on 25 Aug.
```

**After "Not this one"**
```
Set aside. It stays in the record as something you saw and passed on.
```

**After "Ask me again next week"**
```
Back on 4 Aug.
```

**Standing heads** (Archivo 600 `wdth 112` caps `0.12em`)
```
WHAT I WON'T DO WITHOUT YOU
WHY HE THINKS SO
WHAT HE READ
WHAT HE READ LAST NIGHT
ALSO WAITING ON YOU · 1 more · $120 a month
THE RECORD
THE ROAD TO $8,000 A MONTH
NOT ON THIS BAR
IS IT GROWING?
STILL RUNNING
EIGHT MEMORIES BEHIND THIS FIND               (count spelled, from data)
```

**Work order headers** (§1.4, §7.4)
```
Four things I'll draft. None of them are yours to do.
Five steps. Four are mine, one is yours.
Five steps. Three are mine, two are yours.
```

**Links**
```
8 memories, strongest first                →
Read it as written · 214 words             →
Six more lines ⌄                            (exact count, computed)
127 remembered in all                      →
Search everything he's ever seen           →
Open the record                            →
← Back to this morning
```

**The record**
```
6 of 7 calls right · 11 filed · 1 miss, published
$126.50 a day, checked against your real sales
FILED   WHAT IT WAS   HE SAID   IT MADE   HOW
Verified · {note}
Miss · {note}
He wrote this from 2 memories. Every other find here stood on at least 4.
Measuring · {note}
```
Never `86%`. Never `hit rate`. Never a bare percentage anywhere in the product — the owner judge: *"6 of 7 is a thing that happened and 86% is a thing marketers say."*

**The road**
```
$3,795 a month · earning now, checked against your sales
$870 a month · 2 waiting on you
$3,335 a month still to find
NOT ON THIS BAR
4 accepted, being drafted        $2,340 a month   if they hold
4 measured, not yet counted      $3,918 a month   estimated, not verified
Neither is money until the Meter checks it against your sales.
Two months of ledger. A line needs six.        2 / 6
We'll draw the line in October. Until then, two numbers are two numbers.
```

**Evidence**
```
Match is how close a memory sits to the idea. Higher is closer.
Nothing here passes or fails on it.
8 memories, strongest first. 127 remembered in all.
```

**Memory search**
```
Ask what he knows.
Try: why do people leave on Saturdays?
127 things remembered since 2 June.
Nothing matched that. Try fewer words.
```

**Empty state**
```
Nothing needs you this morning.
Radar read 50 new things overnight. None of them are worth your money yet.
We'd rather show you nothing than pad the list.
$126.50 a day is still earning while you read this.
```

**Errors** — every one names what is safe:
```
This morning's page didn't load.
Your ledger is safe — this is the connection, not the record.
[ Try again ]

Couldn't reach the ledger.
Your record is safe. Nothing was sent and nothing changed.
[ Try again ]

Couldn't load the receipts.
The find is here; the memories behind it didn't come through.
[ Try again ]

That didn't go through.
Nothing was drafted and nothing changed. Try once more?
[ Try again ]   [ Not now ]
```

**Loading**
```
READING LAST NIGHT'S WORK
```

**Byline**
```
Radar, Analyst and Meter ran at 4:12am.
```

---

## 13. MOTION

Four motions. There are no others. Nothing fades in on page load — the screen is simply there when it opens.

1. **Weight rules draw** (§8.4). 200ms `cubic-bezier(0.2,0,0,1)`, 20ms stagger by rank, once per find. Functional: it walks the eye down the evidence before a word is read.
2. **Disclosure.** 180ms `cubic-bezier(0.2,0,0,1)`, height only, pushes downward only. Chevron rotates 90°. The row above never shifts.
3. **Footnote focus.** 120ms in / 90ms out, opacity only. The margin never reflows while she reads.
4. **Decision commit.** 260ms. The brass fill of the primary button drains left→right to `#2E5EA8` while the diamond mark morphs to the measuring ring; simultaneously the road's brass region shrinks and the earned region grows, and the masthead count recounts `2 → 1`. **The card does not vanish** — it settles into the record. This is the only animated number in the product, and it animates because something real just changed.

**Banned:** number count-ups (a run-rate animating from zero is a number performing, and this product's premise is that its numbers were checked); scroll-triggered reveals; parallax; skeleton shimmer; hover lift; ambient gradients; page-load choreography of any kind.

**Cut from the winning direction:** the *Dawn* sequence (desk brightening from `#0C0F15`, sheet settling with a growing shadow, "filed 3:14am" fading in last). Both the owner judge and the brand judge named it — *"the one moment of theatre in a design whose whole argument is anti-theatre."* Gone.

`prefers-reduced-motion: reduce` → all four become instant state changes. The weight rules render at full width; the decision commit is a 100ms crossfade. No information lives in motion.

---

## 14. KEYBOARD, FOCUS, ACCESSIBILITY

One of the five directions omitted this entirely and was marked down for it. It is not optional.

**Focus ring:** `outline: 2px solid var(--ink); outline-offset: 2px` on paper (15.16:1); `2px solid var(--on-desk)` on desk (12.03:1). **Zero transition** — a 150ms fade on focus is the difference between an instrument and a toy. Every interactive element has one; nothing relies on `:hover` alone.

**Keyboard** (unadvertised until `?`):
```
J / K      previous / next waiting find
Enter      Yes — draft it for me
L          Not this one
E          open evidence
R          read it as written
/  or ⌘K   search memory
G then M   this morning       G then R   the record       G then D   the road
Esc        back
?          show this
```
Nothing destructive: `Enter` means *draft it for my approval*, which is exactly what the agent's own prose already promises.

**Semantics:**
- Work-order rows are `<button aria-expanded>` controlling a `<div id>`; full text is always in the DOM, so the clamp is invisible to assistive tech.
- The weight rule is `aria-hidden`; the note's accessible name is `` `Memory ${i} of ${n}, ${kindLabel}, ${date}, match ${sim.toFixed(2)}` `` — the mark is a fast path for sighted readers and carries no information the text lacks.
- Every verdict is `<span class="mark" aria-hidden>` + the spelled word in text. A screen reader hears `Miss`, never a colour.
- The road bar is `role="img"` with a full sentence label: *"Of an $8,000 a month goal: $3,795 earning now, $870 waiting on your decision, $3,335 still to find."*
- Live regions: the decision confirmation is `aria-live="polite"`.
- Targets ≥ 44px everywhere; primary decision 56px; no hover-only affordance in the mobile build.
- Text scales to 200% without horizontal scroll: the sheet is `max-width` in `ch`, not `px`, at ≥1.5 zoom.

---

## 15. WHAT WAS CUT, AND WHY

| Cut | Why |
|---|---|
| **Ink-density-as-confidence** (the winner's own signature) | 98 of 108 real similarities fall below 0.45. It would render the product's receipts at 1.9:1–3:1. Contrast is never a data channel here. |
| **Any printed evidence floor** (0.30 or 0.45) | Falsified 9× by the data. A verified win tops out at 0.064; the miss tops out *higher* than a live accepted find. A floor would mark wins as failures. |
| **`AUTO-SPLIT` and every other parser diagnostic** | The owner judge's fatal flaw: software admitting it is guessing at its own writing. Console only. |
| **The colon-split title layout** | 1 of 17 titles has a colon. Four directions built structure on it. |
| **The synthesized bold first clause on step rows** | Produces mid-thought fragments on the real strings. The numeral is the scannable element instead. |
| **The Dawn load animation** | Two judges called it theatre in a design whose argument is anti-theatre. |
| **"Sixty-One Nights" step chart** | Needs a nightly verified series the API does not return (`monthly` has 2 rows; judged events cluster on 7 dates). Replaced by the record list and the refusal block. |
| **The cumulative growth chart, entirely** | Two months is two points. The rejected render's empty plot with a floating goal line read as a broken pipeline. Replaced by a two-row table, a `2 / 6` counter, and a date. |
| **The stacked 44px path-to-goal strip** | The most actionable segment was the least visible. Replaced by a 72px measure where brass is hatched, min-width 48px, carries the only caret, and is the only clickable region. |
| **The ghost extension / in-flight tick on the road bar** | Drawing unverified money as *area* is the projection this product refuses. It is listed in words as `NOT ON THIS BAR`. |
| **Waiting-on-you rows in the record** | Structural requirement of the palette: brass and miss cannot share an encoding channel (ΔE 4.8 deutan / 12.5 normal). Also correct: the record is what happened, not the inbox. |
| **Tabs** | The second rejection. Replaced by a three-item masthead line with a live brass count. |
| **All emoji** | Tofu on Windows Chrome, and `find.emoji` carries less information than the evidence kinds already do. Replaced by `From 4 reviews and 4 local trends`. |
| **`confidence` as a number** | `0.55` is the agent grading its own homework. Evidence count is the honest version. |
| **`hit_rate` as a percentage** | `6 of 7 calls right` is a thing that happened. |
| **Stat-tile rows, donuts, gauges, the ranked bar of source types, the 4.2★ sparkline** | Component-kit reflexes, rejected twice, and none answers a question Rosa asks. |
| **Cream `#F5F1E8` + Fraunces + Inter** | The current identity and, simultaneously, the single most recognisable machine-generated look of the moment. Paper moved to `#F7F5EF`, Fraunces → Newsreader, Inter → Archivo. Brass and the three verdict hues are untouched. |
| **A dark theme** | The verdict palette is validated against paper. The product already contains two materials; a third appearance doubles the QA for no user gain. |
| **A confirmation dialog on "Yes"** | The button says *draft*, not *do*. Nothing irreversible happens, and she knows it because the sentence under the button says so every morning. |

---

## 16. BUILD ORDER

```
src/
  lib/board.ts          §2.2 selectors + money helpers        ← first, with tests
  lib/prose.ts          §7.4 segments() · §7.5 guards() · §7.3 lede()
                                                              ← second, with tests
                          against all 17 fixture moves
  styles/tokens.css     §3 §4 §5 — nothing hardcoded past here
  components/
    Masthead.tsx  Rail.tsx  ProofMargin.tsx  WeightRule.tsx
    FindSheet.tsx  TheAsk.tsx  WorkOrder.tsx  Guards.tsx  TheWhy.tsx
    TheRoad.tsx  Growth.tsx  RecordList.tsx  Verdict.tsx
    EmptyMorning.tsx  SheetError.tsx  Skeleton.tsx
  routes/
    Morning.tsx  Record.tsx  Road.tsx  Evidence.tsx  Reading.tsx  Memory.tsx
```

**Ship order:** `lib/prose.ts` + `FindSheet` + `TheAsk` first, against `8629ea6d` — that is the crux and the demo. Then `ProofMargin`. Then `RecordList` (the miss row is the most persuasive screen in the product). Then `TheRoad` + `Growth`. `Memory` last.

**Tests that must exist before merge:**
1. `segments()` over all 17 moves returns `[]` for the 10 short ones and 4–7 bounded rows for the 7 long ones, in all four shapes. Assert **zero characters lost** — `rows.join('').replace(/\s/g,'')` is a subsequence of `move.replace(/\s/g,'')`.
2. `guards()` returns 3 verbatim clauses for `8629ea6d`, `33d26b20`, `5f1298b8`, `00846dff`, `32745fcf`; returns `[]` for all 10 short moves.
3. No component renders `find.emoji`.
4. Every `<Verdict>` renders both a mark and a word.
5. Road regions sum to `goalMo`, and the overflow branch fires when `earned + waiting > goal`.
6. `todaysFind === null` renders `<EmptyMorning>`, and the masthead brass line is absent from the DOM.