---
name: Brass Tacks
description: An agent with a track record — found money, on autopilot.
colors:
  paper: "#F5F1E8"
  card: "#FFFFFF"
  ink: "#232936"
  ink-soft: "#4A5160"
  muted: "#8B8778"
  line: "#E4DECD"
  brass: "#A8781F"
  brass-deep: "#8A6217"
  gold-light: "#F2D689"
  gold-mid: "#D9A93F"
  verdict-verified: "#118066"
  verdict-miss: "#C0442A"
  verdict-estimated: "#2E5EA8"
  verdict-measuring: "#A8781F"
typography:
  display:
    fontFamily: "Fraunces, Georgia, serif"
    fontSize: "clamp(38px, 6.4vw, 74px)"
    fontWeight: 700
    lineHeight: 1.03
    letterSpacing: "-0.025em"
  headline:
    fontFamily: "Fraunces, Georgia, serif"
    fontSize: "25px"
    fontWeight: 700
    lineHeight: 1.14
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Fraunces, Georgia, serif"
    fontSize: "17px"
    fontWeight: 600
    lineHeight: 1.3
  body:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0.08em"
rounded:
  pill: "999px"
  sm: "8px"
  md: "12px"
  lg: "18px"
spacing:
  xs: "6px"
  sm: "8px"
  md: "16px"
  lg: "22px"
  xl: "44px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.card}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
    typography: "{typography.body}"
  button-primary-hover:
    backgroundColor: "#333B4D"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
  button-commit:
    backgroundColor: "{colors.brass}"
    textColor: "{colors.card}"
    rounded: "14px"
    padding: "13px 20px"
  chip-evidence:
    backgroundColor: "rgba(168,120,31,0.08)"
    textColor: "{colors.brass-deep}"
    rounded: "{rounded.pill}"
    padding: "5px 12px"
  tag-verdict:
    backgroundColor: "{colors.verdict-verified}"
    textColor: "{colors.card}"
    rounded: "{rounded.pill}"
    padding: "3px 8px"
    typography: "{typography.label}"
  nav-item:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    rounded: "13px"
    padding: "7px 16px 8px"
  nav-item-active:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.card}"
  card-surface:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "20px 22px"
---

# Design System: Brass Tacks

## Overview

**Creative North Star: "The Ledger Book"**

This is a bound account book kept by hand. Cream stock, brass fittings, ruled columns,
and a serif that predates screens. The metaphor is not decorative: the product's central
act is writing a prediction down *before* anyone knows the answer, and then coming back
weeks later to record what happened. A ledger is the only object that behaves that way,
and every surface here is a page in a record someone is accountable for.

The register is warm and quiet rather than corporate. The owner meets this at 7am
between other jobs; nothing on the page should feel like it is shouting for attention or
performing urgency, because the work already happened overnight. Density is low, the
paper shows through, and the loudest thing on any screen is a number that has been
verified against real sales.

The one place the system permits itself warmth beyond the page is money: brass and gold
appear only where value is found or a decision is owed. Everywhere else the palette is
paper, ink, and a hairline rule. Confirmed anti-reference: three separate rebuilds of
this interface were rejected for reading as documents *about* an agent rather than a
conversation *with* one. Reports, spec-sheets, and dashboard-chrome are the thing this
world is not.

**Key Characteristics:**

- Paper-first: a cream field with a faint dotted tooth, never a white app canvas.
- Serif carries money; sans carries interface.
- Four reserved verdict colours, colour-vision-validated, always paired with words.
- Ruled hairlines instead of boxes; shadow only where something genuinely floats.
- Physical signatures — a coin jar, a drawn road — that state the product's promise.

## Colors

Paper and ink, with brass reserved for value and a four-colour verdict set that is never
used for anything else.

### Primary

- **Brass** (`#A8781F`): the accent for money found and decisions owed. Evidence chips,
  the commit button on a find, the focus ring, the pill that says something is waiting.
- **Brass Deep** (`#8A6217`): text-weight brass, used when brass has to sit on a tinted
  background and still meet contrast — chip labels, the predicted-but-unverified line.
- **Gold Light** (`#F2D689`) and **Gold Mid** (`#D9A93F`): gradient stops for the coin
  bodies only. These are object colours, not interface colours; they never appear as a
  background, a border, or a text colour.

### Secondary

The verdict set. These four are **reserved** and carry the product's only irreversible
claims. Validated for colour-vision deficiency at ΔE 10.0 under deuteranopia; the green
and red pair they replaced scored 6.0 on precisely this distinction.

- **Verified Green** (`#118066`): money measured against real sales. The headline daily
  figure, verified ledger rows, solid growth bars.
- **Miss Red** (`#C0442A`): a prediction that did not pay. Published, never hidden.
- **Estimated Blue** (`#2E5EA8`): modelled, not measured. Deliberately a different hue
  family from verified so it can never be mistaken for a win.
- **Measuring Brass** (`#A8781F`): a call in flight, outcome unknown.

### Neutral

- **Paper** (`#F5F1E8`): the field colour for every screen. Carries a radial highlight
  and a 22–26px dotted tooth at ~5% ink.
- **Card** (`#FFFFFF`): raised surfaces only — never a page background.
- **Ink** (`#232936`): primary text, and the fill for the active nav item and primary
  button.
- **Ink Soft** (`#4A5160`): secondary prose, body copy in cards.
- **Muted** (`#8B8778`): labels, captions, metadata, timestamps. Warm grey, not neutral
  grey — it belongs to the paper.
- **Line** (`#E4DECD`): every rule, divider and resting border in the system.

### Named Rules

**The Reserved Verdict Rule.** The four verdict colours mean one thing each and are
never borrowed for emphasis, series colour, or decoration. A fifth thing that needs
colour uses brass or nothing.

**The Never-Colour-Alone Rule.** A verdict is always carried by a word as well as a
colour — `VERIFIED`, `MISSED`, `Modelled`, `Measuring`. Removing all colour from any
screen must leave every outcome still readable.

**The Brass Rarity Rule.** Brass marks money or a decision the owner owes. If it appears
anywhere that is neither, it is wrong.

## Typography

**Display Font:** Fraunces (with Georgia, serif)
**Body Font:** Inter (with system-ui, sans-serif)

**Character:** Fraunces is a soft, high-contrast serif with an old-catalogue warmth; it
does the ledger's handwriting. Inter does the interface's clerical work. The pairing is
deliberately lopsided — the serif appears rarely and always on something consequential,
so its arrival reads as significance rather than styling.

### Hierarchy

- **Display** (Fraunces 700, `clamp(38px, 6.4vw, 74px)`, 1.03, −0.025em): the landing
  hero and the running money figure. One per screen, never two.
- **Headline** (Fraunces 700, 25px, 1.14, −0.02em): screen titles — *Every call, and how
  it turned out*, *The road to your goal*.
- **Title** (Fraunces 600, 17–22px, 1.3): find titles, card headings, the goal figure on
  the road.
- **Body** (Inter 400, 13–14.5px, 1.55): all prose, evidence quotations, agent messages.
  Reading columns cap around 62–68ch.
- **Label** (Inter 700, 10.5–12px, 0.04–0.1em, uppercase): verdict tags, eyebrows,
  section kickers, jar captions.

### Named Rules

**The Fraunces-for-Money Rule.** A figure set in Fraunces is a claim about the owner's
money. A figure set in Inter is interface furniture — a count, an index, a percentage of
a chart axis. Never set a projection or an unverified number in the display face at
display size; that is the typographic equivalent of claiming it.

**The Tabular Ledger Rule.** Anywhere two numbers are meant to be compared down a column
— predicted against actual — figures are tabular-lining (`font-variant-numeric:
tabular-nums`) so digits align.

## Layout

A centred single-column document rather than an app shell. Content sits in a
`min(1240px, 96%)` measure on the dashboard and `min(1080px, 100% − 44px)` on the landing
page; the reading and ledger columns narrow further to `min(980px, 100%)` so prose keeps
a comfortable measure.

Spacing runs on a 6 / 8 / 16 / 22 / 44px rhythm. 44px separates major regions (the chat
dock from the chart column), 22px pads raised surfaces, 16px is the default gap inside a
group, and 6–8px binds a label to the thing it labels.

The dashboard is a fixed-viewport application above 900px: the body does not scroll, and
the four screens are absolutely-positioned siblings that swap. At 900px and below the
whole thing becomes an ordinary scrolling document — the header wraps, the nav becomes a
horizontally-scrollable rail, the two-column Manage view stacks, and the hero and jars
flow instead of anchoring. The road keeps a 720px minimum and scrolls sideways inside its
own container rather than compressing its stops.

Charts reserve a 54px axis gutter with hairline rules across the plot area, and a 2px
baseline. Nothing floats without a reference.

### Named Rules

**The One Measure Rule.** Prose never exceeds ~68ch. When a panel is wider than that, the
prose column narrows rather than the text running the full width.

## Elevation & Depth

The page is paper and behaves like paper: **flat at rest, separated by hairline rules
and tonal steps**, not by boxes and shadows. Raised white cards sit on the cream field
with a 1px `Line` border and no shadow at all. Depth is a statement that something has
left the page, and it is reserved for exactly the things that have.

The exception is the object layer. Coins and jars carry inset gradients and drop shadows
because they are physical objects rendered on the page, not surfaces of the interface.
That physicality is theirs alone and does not extend to cards, buttons or inputs.

### Shadow Vocabulary

- **Floating panel** (`box-shadow: 0 28px 64px rgba(35,41,54,0.24)`): the signal
  popover — the only thing that covers content.
- **Decision card** (`box-shadow: 0 18px 50px rgba(35,41,54,0.14)`): the find awaiting a
  choice, lifted off the scene it sits beside.
- **Object** (`box-shadow: 0 6px 16px rgba(35,41,54,0.15)`): road stops and coins.
- **Action lift** (`0 4px 14px rgba(35,41,54,0.16)` → `0 9px 24px rgba(35,41,54,0.22)` on
  hover, with a −2px translate): the landing page's primary call to action.

### Named Rules

**The Flat-Page Rule.** If it is part of the page, it is flat. If it covers the page, it
gets the floating-panel shadow. There is no third tier — do not invent a "slightly
raised" card.

## Shapes

Two radius families, and the choice between them carries meaning.

**Pills (`999px`)** are for state and identity: verdict tags, evidence chips, the run
receipt, count badges, the goal flag, jar captions. If it names what something *is*, it
is a pill. This is the most-used radius in the system by a wide margin.

**Soft rectangles (8 / 12–14 / 18px)** are for surfaces and controls: 8px for small
inlays and swatches, 12–14px for buttons and nav items, 18px for cards and popovers.

Borders are 1px `Line` at rest. The system has no heavy strokes except deliberate object
outlines — the 3px ring on a road stop and the 2.5px stroke on the coin jar, both of
which belong to the object layer rather than the interface.

Curves elsewhere are hand-drawn rather than geometric: the road is a cubic bezier with a
dashed centre line, and the jar silhouette is a drawn path. Nothing in the system uses a
perfect circle except a coin.

### Named Rules

**The Pill-Means-State Rule.** A pill labels a condition. A rounded rectangle holds
content or takes an action. Do not put a pill radius on a container.

## Components

### Buttons

- **Shape:** soft rectangle, 12px (`{rounded.md}`); the commit button on a find is 14px
  and larger because it is the only irreversible-feeling action on the screen.
- **Primary:** `Ink` fill, white text, 8px × 16px padding, 13px Inter 600. Hover lightens
  to `#333B4D` over 150ms.
- **Commit (find):** `Brass` fill, white text, 13px padding, 15px Inter 600. Used only
  for *Do it now* and *Do it — draft it for me*.
- **Ghost:** transparent, `Muted` text, `Line` border. Used for *Later*, *Not now*,
  *Start over* — every reversible choice.
- **Focus:** 3px `Brass` ring at 3px offset, on every interactive element in the system.
- **Disabled:** 0.45 opacity, default cursor. No colour change.

### Chips

- **Evidence chip** (`📎 9 memories behind this ›`): pill, `Brass` at 8% fill, 22% border,
  `Brass Deep` label, 12px Inter 600. Hover deepens the fill to 16%. This is a disclosure
  control, not a tag — the arrow flips to `⌄` when open.
- **Run receipt**: same geometry in ink rather than brass (`rgba(35,41,54,0.05)` fill,
  12% border, `Ink Soft` label, weight 500), because it reports process rather than
  offering evidence.
- **Verdict tag**: pill, solid verdict fill, white uppercase label at 10.5px / 0.04em.

### Cards / Containers

- **Corner:** 18px.
- **Background:** `Card` white on the `Paper` field.
- **Shadow:** none at rest — see Elevation.
- **Border:** 1px `Line`.
- **Internal padding:** 20–22px.

### Inputs / Fields

- **Style:** pill (999px), `Card` fill, 1px `Line` border, Inter 14px, generous 12–14px
  vertical padding. The chat composer is the canonical instance.
- **Focus:** the system focus ring; no colour shift on the field itself.

### Navigation

The nav **is** the product's loop, not a menu: Manage → Autopilot → Radar → Ledger,
separated by `→` glyphs and closed by a `↻`. Each item is a two-line pill carrying its
own live state (`127 remembered`, `+$126.50/day`, `6 to review`, `6 of 7 paid`), so the
navigation doubles as the status bar. Active state is a solid `Ink` fill with white
label. Below 900px the arrows are hidden and the rail scrolls horizontally.

### Signature Components

- **The coin jar.** A drawn glass jar with a `#EFE8D5` lid, holding gold coins at six
  fixed resting positions with slight rotations. Verified finds rest solid; a coin the
  owner has just accepted is dashed in `Brass Deep` and counted separately in the caption
  (*Earning now · 2 measuring*) until the Meter verifies it.
- **The road to the goal.** A cubic-bezier path with a dashed centre line, running from a
  `📍 Today` marker to a goal flag. Stops are 48px circles on the curve; labels alternate
  above and below so neighbours cannot collide, lead with the money figure so truncation
  can never eat it, and ellipsise rather than clip.
- **The ledger row.** Predicted over actual as two bars on a shared scale, so the gap is
  seen rather than computed. The second bar is labelled *Modelled* rather than *Actual*
  when the verdict is an estimate.

## Do's and Don'ts

### Do

- **Do** pair every verdict colour with its word. The screen must survive being read in
  greyscale.
- **Do** set money in Fraunces and interface numbers in Inter — the face is the claim.
- **Do** use pills for state and soft rectangles for surfaces.
- **Do** keep raised surfaces flat with a 1px `Line` border; spend shadow only on the
  popover, the decision card, and the object layer.
- **Do** show retrieval similarity as a number (`0.743`) beside the row it belongs to.
- **Do** put a visible 3px `Brass` focus ring on anything clickable, including custom
  controls drawn as circles on a curve.

### Don't

- **Don't** encode similarity, confidence, or strength as opacity, weight, or colour
  intensity. In this dataset two verified wins have weaker top evidence than the
  published miss; any intensity ramp would tell the owner something false.
- **Don't** borrow a verdict colour for decoration, series colour, or emphasis.
- **Don't** draw a chart element the database cannot support. A panel with no data is
  deleted, not filled with a plausible shape.
- **Don't** let an interface action move a verified figure. Accepting a find moves the
  predicted line and nothing else.
- **Don't** put gold on anything that is not a coin.
- **Don't** add a third elevation tier.

### Known debt

**Iconography is emoji, and that is incumbent rather than intended.** Emoji currently
carry find identity, nav labels, and verdict marks. They render differently per platform,
cannot be styled or aligned reliably, and are the system's weakest craft signal. The
replacement — a single-weight drawn line set at 16/20/24px inheriting `currentColor` — is
the top outstanding item, recorded here so future work has a target rather than a
decision to re-litigate. Until it ships, emoji usage stays as-is; do not partially
replace them, which would leave two icon languages on one screen.
