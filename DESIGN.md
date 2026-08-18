---
name: Brass Tacks
description: An agent with a track record — found money, on autopilot.
colors:
  paper: "#F5F1E8"
  paper-on-ink: "#FBF9F4"
  ink-hover: "#333B4D"
  card: "#FFFFFF"
  ink: "#232936"
  ink-soft: "#4A5160"
  muted: "#6E6A5C"
  line: "#E4DECD"
  brass: "#A8781F"
  brass-deep: "#8A6217"
  gold-light: "#F2D689"
  gold-mid: "#D9A93F"
  verdict-verified: "#118066"
  verdict-verified-ink: "#0D6450"
  verdict-measuring-ink: "#8A6217"
  verdict-miss: "#C0442A"
  verdict-estimated: "#2E5EA8"
  verdict-measuring: "#A8781F"
typography:
  display:
    fontFamily: "Newsreader, Georgia, serif"
    fontSize: "clamp(38px, 6.4vw, 74px)"
    fontWeight: 700
    lineHeight: 1.03
    letterSpacing: "-0.025em"
  headline:
    fontFamily: "Newsreader, Georgia, serif"
    fontSize: "25px"
    fontWeight: 700
    lineHeight: 1.14
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Newsreader, Georgia, serif"
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
  marketing-display:
    fontFamily: "Newsreader, Georgia, serif"
    fontSize: "clamp(42px, 7vw, 86px)"
    fontWeight: 700
    lineHeight: 1.0
    letterSpacing: "-0.03em"
  marketing-head:
    fontFamily: "Newsreader, Georgia, serif"
    fontSize: "clamp(28px, 3.8vw, 44px)"
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: "-0.02em"
  marketing-body:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "17px"
    fontWeight: 400
    lineHeight: 1.6
  marketing-label:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "0.11em"
  marketing-lead:
    fontFamily: "Newsreader, Georgia, serif"
    fontSize: "clamp(24px, 3vw, 34px)"
    fontWeight: 600
    lineHeight: 1.15
    letterSpacing: "-0.015em"
  marketing-stat:
    fontFamily: "Newsreader, Georgia, serif"
    fontSize: "clamp(34px, 4.4vw, 52px)"
    fontWeight: 600
    lineHeight: 1.0
    letterSpacing: "-0.025em"
  marketing-figure:
    fontFamily: "Newsreader, Georgia, serif"
    fontSize: "32px"
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: "-0.02em"
  marketing-title:
    fontFamily: "Newsreader, Georgia, serif"
    fontSize: "21px"
    fontWeight: 600
    lineHeight: 1.25
  marketing-sub:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.55
  marketing-fine:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
rounded:
  xs: "4px"
  sm: "8px"
  md: "12px"
  commit: "14px"
  lg: "18px"
  pill: "999px"
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
    rounded: "{rounded.md}"
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

**Mark colours versus ink.** The four above are *marks* — bars, dots, fills — and are
the exact CVD-validated set. Set as text on paper, verified measures 4.33:1 and
measuring 3.47:1, both under AA, so two ink variants exist: **Verified Ink**
(`#0D6450`, 6.30:1) and **Measuring Ink** (`#8A6217`, 4.85:1). Do not "fix" the mark
colours by darkening them — dropping verified to `#0F7259` was tried and takes chroma
under the floor while collapsing CVD separation from ΔE 10.0 to 6.7. Verified with
the palette validator.

### Neutral

- **Paper** (`#F5F1E8`): the field colour for every screen. Carries a radial highlight
  and a 22–26px dotted tooth at ~5% ink.
- **Card** (`#FFFFFF`): raised surfaces only — never a page background.
- **Ink** (`#232936`): primary text, and the fill for the active nav item and primary
  button.
- **Ink Soft** (`#4A5160`): secondary prose, body copy in cards.
- **Muted** (`#6E6A5C`): labels, captions, metadata, timestamps. Warm grey, not neutral
  grey — it belongs to the paper. It was `#8B8778` until a contrast pass: that value
  measured 3.19:1 on paper and failed AA in every place it was used.
- **Line** (`#E4DECD`): every rule, divider and resting border in the system.

### Surface tints

Repeated tints were literals scattered across the sheet until a consolidation pass.
They are tokens now, and a second red (`#C05B4D`) that had been living beside the
validated miss colour was folded into it — two reds a few points apart, on the one
distinction this product exists to make.

- **Line Soft** (`#F0EDE5`) — the lightest divider, inside a panel.
- **Line Firm** (`#C9C2B0`) — a border under hover or emphasis.
- **Brass Tint** (`#F6EFDF`) / **Brass Line** (`#E3D2A8`) — the warm fill and border
  used when something is waiting on the owner.
- **Shadow 1 / 2 / 3** (`rgba(35,41,54,0.05 / 0.12 / 0.18)`) — the only three shadow
  alphas in the system.

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

**Display Font:** Newsreader (with Georgia, serif)
**Body Font:** Inter (with system-ui, sans-serif)

**Character:** Newsreader is an old-style serif drawn for screen reading — sharper
serifs, more ink on the page, and figures that sit like a printed account book's.
It replaced Newsreader, which read softer and is one of the handful of faces every
recent AI-built interface reaches for. Inter does the interface's clerical work. The pairing is
deliberately lopsided — the serif appears rarely and always on something consequential,
so its arrival reads as significance rather than styling.

### Hierarchy

- **Display** (Newsreader 700, `clamp(38px, 6.4vw, 74px)`, 1.03, −0.025em): the landing
  hero and the running money figure. One per screen, never two.
- **Headline** (Newsreader 700, 25px, 1.14, −0.02em): screen titles — *Every call, and how
  it turned out*, *The road to your goal*.
- **Title** (Newsreader 600, 17–22px, 1.3): find titles, card headings, the goal figure on
  the road.
- **Body** (Inter 400, 13–14.5px, 1.55): all prose, evidence quotations, agent messages.
  Reading columns cap around 62–68ch.
- **Label** (Inter 700, 10.5–12px, 0.04–0.1em, uppercase): verdict tags, eyebrows,
  section kickers, jar captions.

### Two surfaces, two scales

The dashboard is **Operate** — the visitor is completing a task at 7am, so type is
small, dense and quiet, and the ramp above governs it. The landing page is
**Persuade**, where the job is a decision, and it runs a separate marketing ramp:
display `clamp(42px, 7vw, 86px)`, head `clamp(28px, 3.8vw, 44px)`, body 17px, label
12px, with 13/15/21/32px as the only supporting steps.

That page carries the larger scale because the reference set is unanimous about it:
of the fourteen shipped sites surveyed during design research (screenshots since
removed from the repo — they were third-party pages), every one leads with
an oversized headline, one line of subcopy and a single button. **Paper on Ink**
(`#FBF9F4`) is the off-white used for text sitting on the ink-filled primary button —
pure white on ink reads colder than this world wants.

### Named Rules

**The Serif-for-Money Rule.** A figure set in the serif is a claim about the owner's
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

## Onboarding

The signup experience is a workspace setup, not a generic account form. It has two short
steps and a live agent brief that explains how each answer changes the system:

- **Business** defines the Radar search boundary: category, place, and an optional known URL.
- **Buyers** defines Analyst relevance: buyer segments, core offers, channels, and one goal.
- **Ready** confirms the exact profile that will scope the first market sweep.

On desktop, the form and agent brief sit side by side. On narrow screens, the form remains
first and the brief follows it. Required fields are limited to what the agents can use. The
interface never implies that an account was securely provisioned when only browser storage is
available; local and connected modes are stated in plain language. A newly created workspace
starts at zero observations and never borrows the fictional demo tenant's cards, growth, or
ledger.

## Do's and Don'ts

### Do

- **Do** pair every verdict colour with its word. The screen must survive being read in
  greyscale.
- **Do** set money in Newsreader and interface numbers in Inter — the face is the claim.
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

### Iconography

**Interface icons are a drawn set; find emoji are content.** The line is deliberate and
worth keeping:

- **Drawn icons** carry every interface job — the four loop tabs, the four vitals, the
  evidence clip, close, send, the road's start pin and goal flag. One weight (1.7),
  a 24px grid, `currentColor`, no fills, defined once as `<symbol>` and referenced with
  `<use>`. They inherit text colour, so they take state for free.
- **Emoji stay on finds.** Each find's emoji comes from the database — the Analyst
  chooses it — and it is that find's identity as it moves from the road to the jar to
  the ledger. Replacing it would mean changing the agent's output schema, and a drawn
  icon cannot say *tiramisu*.

Emoji also survive in the agent's chat messages, where they are voice rather than
iconography. Use them sparingly there and never as the only carrier of meaning.

**Do not add a second icon language.** If a new interface icon is needed, draw it on the
same grid at the same weight and add it to the sprite.
