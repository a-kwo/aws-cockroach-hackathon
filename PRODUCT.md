# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The primary user is the owner-operator of a small business — someone who works *in*
the business most of their waking hours and has nobody working *on* it. They are not
an analyst, they are not looking at a dashboard by choice, and they open this at
around 7am between other jobs, on whatever device is nearest.

The demo tenant is a restaurant (Rosa's Trattoria, fictional), and restaurant
vocabulary — covers, service, menu, waitlist — is the language the seeded corpus and
the Analyst's queries currently speak. **The product is aimed at small businesses
generally, with restaurants as the demonstration category.** The schema carries a
`category` field for that reason.

Secondary audience for the current cycle: hackathon judges, who read the repo, run the
demo, and watch a three-minute video. Their needs shape what is *visible*, not what is
true.

## Product Purpose

A team of agents works overnight for the owner. Each night it observes what changed —
reviews, competitor prices and menus, local trends — searches everything it has ever
learned about that business, and proposes one revenue move with a dollar figure
attached and a date to check it. Weeks later, a later run reads that stored prediction
and compares it against what actually happened.

Success is a track record: a verdict on every call, including the ones that failed.

## Positioning

The Meter judges predictions made on earlier nights by agent runs that no longer
exist. That is the mechanism a neighbouring product cannot truthfully copy without
building the same durable memory: **a stateless advisor can give advice forever and
never be wrong, because nothing it said was ever written down.**

The second half of the position is the harder one to imitate, because it is a choice
rather than an architecture: the misses are published on the same ledger as the wins,
in the same place, never sorted away.

## Operating Context

- The loop runs unattended overnight on a schedule, not on a button. The owner is
  asleep when the work happens.
- The owner meets it in the morning with minutes, not hours. A find is a two-tap
  decision: do it now, or save it for later.
- Verify windows are 14–21 days, so the gap between a claim and its verdict is weeks.
  The product has to hold state across that gap and stay legible while it does.
- Owner rules constrain the agents on every run — standing constraints such as *never
  change prices without asking me first* and *draft everything, nothing sends without
  my OK*. The agent proposes and drafts; the owner decides.
- The current public demo is read-only: reads come from the live cluster, decisions
  live in the browser and are discarded on refresh.

## Capabilities and Constraints

**Confirmed capabilities.** Nightly observation with content-hash dedup; embedding of
every observation; vector search across the full accumulated corpus before any
recommendation; a validated find with a dollar prediction, confidence, and verify-by
date; an evidence trail recording which rows retrieval returned and at what similarity;
a verdict of verified / estimated / miss written to a permanent ledger.

**Technical constraints.**

- CockroachDB is the only store. No second store for vectors, sessions or state.
- Amazon Bedrock (Titan Text Embeddings V2, 1024 dims) generates every vector.
- Reasoning runs on the Anthropic API, not Bedrock — AWS cannot grant this account any
  current Claude model. This is forced, not preferred, and is disclosed publicly.
- Money is stored and computed as integer cents. Never floats.
- Retrieval requires concrete, hypothesis-shaped queries; abstract ones measurably
  retrieve the wrong material. The Analyst issues six concrete questions per night.
- Test-driven development is required, and the unit suite must run offline with no
  cloud account.

**Explicitly undecided.** Pricing, packaging, and whether any category beyond
restaurants gets a real corpus. Accounts, multi-tenancy and billing are deliberately
out of scope for this cycle. Whether the product continues after 18 August 2026 is
undecided — the current cycle is scoped to the submission.

## Brand Commitments

- **The name is Brass Tacks**; the line that goes with it is *"Found money, on
  autopilot."*
- **The visual world is the incumbent one and is binding.** Cream paper, brass, a
  Fraunces display face with Inter for body and data. Its signature devices — the jar
  the coins drop into, the illustrated road to the goal, and the agent as a
  conversation rather than a report — were chosen by the owner over three separate
  rebuilds. Refinement preserves them.
- **Verdict colours are fixed and validated:** verified `#118066`, miss `#C0442A`,
  estimated `#2E5EA8`, measuring `#A8781F`. The original green/red pair failed
  colourblind separation (ΔE 6.0 under deuteranopia) on the single most important
  distinction in the product; these score ΔE 10.0. Verdict is never carried by colour
  alone.
- **Voice:** plain, specific, unhedged. It names amounts and dates. It says *I don't
  know* rather than inventing a number, and it never apologises at length.

## Evidence on Hand

Real, and queried live from a CockroachDB cluster on AWS us-east-1 — not typed into
the markup:

- 127 stored observations across 5 signal kinds; 15 finds; 92 evidence rows carrying
  cosine similarity to the query that retrieved them.
- A ledger of 6 verified, 1 miss, 1 estimated, 1 still measuring — an 86% hit rate at
  $126.50/day verified.
- The published miss is real and named: *Espresso upsell after dessert*, predicted
  +$12.00/day, actual $0.00, pulled after four days.
- The six hypothesis queries the Analyst issues are shown verbatim in the UI and are
  kept in sync with the backend by a test.

**Absences future work must not fabricate.** Rosa's Trattoria is a hand-written
fiction and must never be presented as a real customer. There are no real customers, no
testimonials, no press, no pricing, no benchmarks, no peer or competitor data, and no
P&L for the demo tenant. The mock this frontend descends from was generated with AI and
then refined by hand; that, and the Bedrock model-access constraint, are disclosed
rather than hidden.

## Product Principles

1. **Never claim money that has not been measured.** Only a verified verdict moves the
   record. Estimates are labelled as modelled, projections name what they are waiting
   on, and nothing the owner does in the interface can raise the verified figure.
2. **Publish the misses, in the same place as the wins.** A record that only contains
   successes is marketing.
3. **Every recommendation carries its retrieval trail**, attached to the claim it
   produced rather than filed behind a tab.
4. **Nothing appears on screen that is not a row in the database.** If the data cannot
   support a panel, the panel does not exist.
5. **The agent drafts; the owner decides.** Standing owner rules bind every run, and
   the agent says *I don't know* rather than inventing a fact it does not hold.

## Accessibility & Inclusion

**Target: WCAG 2.2 AA.**

Already true: the verdict palette is validated for colour-vision deficiency and is
always paired with a text label; a visible focus ring covers interactive elements; the
road stops are real buttons with descriptive labels; reduced motion is respected.

Known gaps to close against that bar: a full contrast pass on secondary text and the
chart captions, keyboard reachability of the remaining custom controls, and screen
reader labelling for the jar coins and the chart bars. Similarity is shown as a number
and never as opacity or colour intensity — partly a truth constraint, and partly
because the encoding it replaced rendered most evidence at roughly 2:1 contrast.
