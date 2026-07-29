# UI improvement plan

> **Status: executed 2026-07-28.** Everything in P0, P1, P2 and L1 below is built and
> verified in a browser. What changed beyond the plan, because it was found while
> building:
>
> - The mock's **Health**, **Money** and **Reputation** panels were fabricated —
>   invented health scores against invented peers, a week of invented nightly revenue,
>   a rival leaderboard with invented star counts, and a drafted reply to a customer who
>   does not exist. Replaced with observation counts by kind, the real predicted-vs-actual
>   ledger, and the weekly review average computed in SQL.
> - **Answering a chat question grew the money bar by $500.** The record now only moves
>   when the Meter verifies something.
> - **Accepting a find bumped the verified daily rate.** It now moves a separate
>   *predicted* line, and the coin is dashed until verified.
> - Two invented signals were pushed onto the road on a timer (a rival's brunch launch,
>   a fresh five-star review). Removed.
> - The find queue was hardcoded for exactly two finds and stopped after the second.
> - `Product Demo/` is now frozen; the shipped source is `site/`, and `CLAUDE.md`'s
>   provenance note has been corrected — it used to claim no code from the mock ships.
>
> 29 new tests in `backend/tests/test_site_build.py` cover the money formatting, the
> agent-prose splitting, and the honesty invariants. Suite: **194 passing.**

Written 2026-07-28. Feature freeze is **Aug 14**, so everything here is sized to fit
before it. This is a plan to improve the mock we adopted — not another redesign.
Three redesigns were already rejected; the mock won because it reads as a conversation
with an agent rather than a document about one. Nothing below changes that.

Grounded in two things: screenshots of our own three screens as they render today with
live data, and four products looked at directly for the specific problems we have.

---

## Part 1 — What our UI actually does today

Not from memory. From opening `http://127.0.0.1:8901` and looking at all three tabs.

### The core problem: it is an animated demo, not a dashboard

On load, before pressing **Run autopilot**:

| Screen | What a first-time visitor sees |
|---|---|
| **Autopilot** | Two **completely empty jars**. Caption reads "Earning now" and "Saved for later · +$0/day waiting" — while the header pill directly above says **+$126/day**. |
| **Manage** | Agent says three sentences, then ~250px of void, then "3 inputs wanted" stranded at the bottom of the column. |
| **Radar** | Four stops crammed into the lower-left; the right two-thirds of the road is empty; ~200px of dead space above it. |

State only materialises when you press play. **A judge who opens the demo URL and does
not press play sees an empty product.** That is the single highest-cost problem here,
and it is also the cheapest to fix.

### Real data breaks the layout

The road labels collide and truncate:

- `he Saturday... +$750` — the leading "T" is clipped by the stop before it
- `TC...` at the first stop, overlapped by the `+$3,540/mo` figure and the "Today" pin

`build_web.py` places stops at `t = 0.18 + 0.64 * i / total`. With `total = 3` that is
t = 0.18, 0.39, 0.60 — every stop in the left 60%, and because the road is an S-curve
the visual x-position bunches tighter still. The mock was authored for 5 evenly-spread
stops with short labels; we hand it 3 long ones.

### The screen we do not have is the one we would win on

We hold **6 verified, 1 published miss, 4 measuring, and 108 evidence rows carrying
similarity scores**. On screen, all of that is represented by a single road stop with a
`✓` on it. There is no ledger view. The strongest claim in the entry — *this agent has a
track record, including a failure it published* — is currently invisible.

### The dashboard contradicts itself

Three statements, visible simultaneously on Manage:

- header pill — **+$126/day**
- agent banner — **🔴 4 areas red.**
- footer — **6 of 7 calls verified**

Nothing reconciles these. The banner is the loudest and it says everything is broken.

### The growth chart is five-sixths speculation

`brasstacks-jar-demo.html:1589–1594`: one live bar (`$780`) followed by five hardcoded
ghost bars — `~$2.4k`, `~$3.6k`, `~$4.7k`, `~$5.6k`, `~$6.4k`. The caption says
"dashed = projected", which is honest wording, but the eye reads six bars of growth and
five of them are invented.

We actually have **two real months** in `demo.json.monthly`: June +$66.00/day, July
+$60.50/day, cumulative $126.50/day. We are drawing five fictional futures over two real
months of evidence, inside a product whose entire pitch is that it publishes its misses.
This is the one item on this list that is a credibility risk rather than a polish item.

---

## Part 2 — What the reference products do

Four products, chosen because each has already solved a problem we currently have.

### Linear — the agent reports as a receipt

Their hero is an agent working, and the panel reads:

```
Examining the startup path…
Worked for 7s  ▾
Pushed and opened a draft PR. Changes:
 • useRideHistory.ts : build a waitingStatusById map …
 • RideHistoryPage.tsx : dimmed rows reset
Changed 2 files  +4 −4        [ Preview ]
```

Four things worth stealing:

1. **The output is a list of concrete artifacts, not prose.** Every line names a thing
   that was touched. Our Analyst emits twelve lines of paragraph and `shorten()` throws
   eleven of them away.
2. **`Worked for 7s ▾`** — the process is collapsed by default but present and
   expandable. Effort is stated as a fact, one line, no drama.
3. **`Changed 2 files +4 −4`** — the work is quantified before you open it.
4. **No region is empty.** Rail, content, metadata panel, agent card — every area
   carries information at rest.

### Perplexity — citations live at the claim

1. **`Sources 9 ›`** pinned top-right, visible before you read a single word.
2. **Inline chips at the end of each sentence** — `qsrautomation +1` — naming the source
   and the overflow count, attached to the claim that needs it.
3. **`Searching the web ›`** — the retrieval step as a collapsed line above the answer.
4. The answer is split **"When it helps" / "When it may not."** Both sides, structurally.

Our `find_evidence` table is exactly this data and it is buried one click deep.

### Ramp — a claim never travels without its receipt

The eyebrow above the headline is a live ticking number. Further down: *"growing 3.2x
faster than the average American business"* immediately followed by **Read the report →**.
Every number is either live or linked to its proof.

### Metaculus — a track record is a chart, not a number

Their calibration curve plots predicted probability against actual resolution rate with
a diagonal reference line. Too statistical for a restaurant owner, but the principle
transfers: **a hit rate stated as "86%" is a claim; predicted-vs-actual plotted per find
is evidence.** We have the data for the second and are currently only showing the first.

---

## Part 3 — The plan

Ordered by value per hour. P0 is demo-critical.

### P0 — before the video

**1. Fill every screen on load.** *(highest value, lowest cost)*
Pre-populate the jars, the road and the chat from `demo.json` at render time. Demote
**Run autopilot** from "the only way to see anything" to "replay tonight's run." The
animation stays — it is good, and it is the money shot in the video — but it becomes a
replay of state that is already true, not the thing that creates it.
*Reference: Linear, where no region is empty at rest.*

**2. Build the Ledger screen.** *(the missing screen)*
A fourth nav pill after Radar. One row per judged find, most recent first:

```
🍰  Reprice the tiramisu           predicted +$23.00/day    actual +$27.30/day    VERIFIED
☕  Espresso upsell at the table   predicted +$18.00/day    actual  $0.00/day     MISS
```

Predicted and actual as a paired horizontal bar per row so the gap is visible without
arithmetic. Sort by date, never by outcome — burying the miss would defeat the point.
Header states the record plainly: **6 verified · 1 miss · 4 still measuring.**
Use the CVD-validated palette (`#118066` / `#C0442A` / `#2E5EA8` / `#A8781F`), not the
original green/red pair.
*Reference: Metaculus — plot the prediction against the outcome.*

**3. Make the growth chart honest.**
Replace the five hardcoded ghost bars with the real `monthly` aggregate: two solid bars
(Jun, Jul) and **at most one** dashed projection, labelled *"if the 4 finds still
measuring land as predicted."* A projection tied to named pending finds is a forecast; a
five-bar ramp to `~$6.4k` is a hockey stick. Add a baseline and a y-axis — the bars
currently float with no reference.

**4. Fix the road.** Distribute stops across the full `t` range instead of `0.18–0.82`
scaled by count, alternate labels above/below the curve, and cap label width with a
proper ellipsis so nothing is clipped mid-word. Verify with the real 3-stop dataset,
not a hypothetical 5.

**5. Resolve the contradiction.** Retire the scripted `🔴 4 areas red` banner and the
scripted `72% mapped`. One status line, computed: *"$126.50/day earning · 2 finds waiting
on you · 4 still measuring."* If a dimension genuinely is red, it should be red because
the data says so, and it should not shout over a verified track record.

### P1 — the receipts (this is what the judging question asks about)

**6. Evidence chips at the claim.** On every find, inline after the recommendation:
`📎 6 memories ›`. Clicking expands the actual observation rows with dates and source
kinds. This is `find_evidence` — the table that proves retrieval drove the reasoning —
placed where the claim is made rather than behind a tab.
*Reference: Perplexity's inline source chips.*

**7. A run receipt, collapsed.** Above each find, one line in the Linear register:
`Read 127 memories · 6 hypotheses searched · best match 0.583 ▾`
Expanded, it shows the six concrete hypothesis queries and what each returned. That
single line does more to prove the memory layer is load-bearing than any diagram, and
it is the natural thing to point a camera at.

**Encoding rule for both:** rank order and plain words only. Never encode similarity as
opacity or color intensity — 46% of our evidence rows sit below 0.30, and two verified
wins have *weaker* top evidence (0.064, 0.157) than the published miss (0.315).
Similarity does not predict outcome, and the UI must not imply that it does.

### P2 — after the demo works

**8. Structured find bodies.** Render the Analyst's `move` as bullets, each naming one
concrete action, instead of truncating twelve lines to one at the first clause.
`shorten()` is a workaround for a rendering gap; Linear shows the fix.

**9. Mobile.** Unverified at any width. Rosa reads this on a phone at 7am.

---

---

## Part 4 — Landing page, no login

Requested 2026-07-28, revised the same day to **drop the login gate entirely**: the
landing page has a *Try the demo* button that goes straight into the dashboard. No
signup, no password, no session.

This is the better call and it restores the `CLAUDE.md` scope cut ("Dropped: Stripe,
accounts, multi-tenancy. Single demo tenant") rather than reversing it. It removes about
three and a half days of work — password hashing, session storage, a Lambda authorizer,
tenant-isolation test coverage — from a schedule that still owes deployment, the Ask
agent, and the README before the **Aug 14 freeze**. None of that work was required by the
rules; all of what it displaces is.

It also removes the risk that mattered most: a judge meeting a registration form instead
of the product.

### The one thing that has to come with it

Removing the login is only safe if **the deployed API has no public write path.** A login
wall in front of a writable API and no wall at all in front of a writable API differ by
one accident. Concretely:

- The public API is **read-only**. It serves the dashboard's data and nothing else.
- **No endpoint triggers an agent run.** The nightly loop is invoked by EventBridge
  Scheduler calling the Lambdas directly — it is never reachable through API Gateway.
  A public "run the agents" endpoint is a button that spends Bedrock and Anthropic
  budget once per click, from anyone, forever.
- **"Run autopilot" is a client-side replay** of state already in the database. It is
  already built this way in the mock; it must stay that way once deployed.
- Throttling and a short cache TTL on API Gateway, so the read path cannot be used to
  hammer the cluster either.

With those four in place, the entire abuse surface is "someone reads the demo tenant's
data," which is exactly what we are publishing anyway.

### How interaction survives without accounts

The interesting interactions — accepting a find, saving one for later, running the
autopilot animation — all still work, because **decisions stay in the browser and are
never written to CockroachDB.** Reads are live from the cluster; writes are local state,
and a refresh resets them. The React build already did this with `useState`, and the
mock's animation is already client-side, so this is the existing behaviour rather than
new work.

Disclose it in the UI, in the footer that already reads *"Live data · 127 observations ·
6 of 7 calls verified"*: add *"· your changes stay in this browser."* One clause, and
nobody mistakes a demo click for a change to a real business.

### What is dropped from the earlier draft

`account` and `session` tables, Argon2id hashing, opaque session tokens, the cookie
policy, the API Gateway authorizer, login rate-limiting, and the read-only session flag.
All of it — the `read_only` column existed only to stop one visitor's clicks from
changing what the next visitor sees, and client-side state solves that for free.

**Keep** the cheap repository-scoping tests: every read is scoped by `business_id`, and
vector search never crosses the tenant boundary even when another tenant's rows are the
nearest neighbours. There is one tenant today, so these are guarding the future rather
than the present, but they are a few lines each and the vector index is already prefixed
by `business_id`.

**If lead capture is wanted later**, an email field on the landing page is one table and
no auth — but it is PII in the cluster and needs rate-limiting and a line in the README
about retention. Genuinely optional; not planned here.

### L1 — The landing page

Two routes, both public:

| Path | What it is |
|---|---|
| `/` | the landing page |
| `/app` | the dashboard — the existing `web/index.html` |

**The hero should be our real record, not a value proposition.** This product's most
distinctive asset is that it publishes its failures, so the honest thing is also the
strongest marketing:

> **Six of our last seven calls paid.**
> Here is the one that didn't. ☕ Espresso upsell — predicted +$18.00/day, actual $0.00.

Sections below the hero, each showing a real artifact rather than an icon:

1. **The loop** — Radar → Analyst → Meter as three steps, each illustrated with an
   actual row: a real observation, a real find with its evidence chips, a real ledger
   verdict.
2. **The receipt** — one find fully expanded: the six hypothesis queries, the retrieved
   observations with dates and similarity, the prediction, the outcome. This is the
   "show, don't tell" section and it doubles as the thing the demo video points at.
3. **Plain terms** — what it does overnight, what it asks of you in the morning.
4. **One call to action** — *Try the demo →*, repeated at the top and bottom, going
   straight to `/app`. One button, one destination, no form. A second CTA competing with
   it would only dilute the one thing we want a judge to do.

Build it by extending `build_web.py` to emit `web/landing.html` from the same
`demo.json`. The landing page's numbers are then generated from the cluster, so it
**cannot drift from the product** — a marketing page that is structurally incapable of
overstating the record. Same cream/brass tokens, same Fraunces, same dotted texture. No
new visual language; this is a fourth page in the existing one.

### L2 — The read-only API contract

The dashboard is served as static HTML with data spliced in at build time today, and
that can survive deployment: `build_web.py` regenerates `web/` after each nightly run,
and the pages go to S3. No API is strictly required for the demo.

If a live API is added anyway (worth it, so the page reflects the cluster without a
rebuild), it exposes exactly these, all `GET`:

| Endpoint | Returns |
|---|---|
| `GET /api/summary` | ledger counts, verified daily rate, goal, corpus size |
| `GET /api/finds` | finds with their evidence rows and similarity scores |
| `GET /api/monthly` | the real per-month aggregate behind the growth chart |

No `POST`, no `PUT`, no `DELETE`, no path that starts an agent run. `business_id` is
resolved server-side from configuration — never read from a query string or body, so
there is no parameter to tamper with.

### L3 — Rules that must not be broken

- The nightly loop is reachable **only** from EventBridge Scheduler, never through
  API Gateway.
- No deployed endpoint calls Bedrock or the Anthropic API on request. Model spend is
  driven by the schedule, on our cadence, not by traffic.
- API Gateway throttling and a short cache TTL on every read route.
- `db/fixtures/demo.json` is committed to the repo — `export_fixture.py` must
  never export anything that is not already public. That rule outlives this decision.
- Seed and demo data carry no real names, emails, or third-party trademarks.

### Effort

| Item | Estimate |
|---|---|
| L1 landing page | ~1 day |
| L2 read-only API, if added | ~0.5 day |

About three and a half days cheaper than the account-based draft, and the saved time
goes to deployment and the Ask agent — both required by the rules, unlike accounts.

---

## What is deliberately not in this plan

- **No redesign.** The visual language, the jar metaphor, the road, and the chat-first
  posture all stay exactly as they are — the landing page included.
- **No new palette.** The validated four-color set is already chosen; the Ledger just
  needs to use it.
- **No new framework.** Everything above is edits to the mock plus `build_web.py`. The
  React build stays unused.
- **No accounts, no login, no billing.** Per `CLAUDE.md`: single demo tenant. The landing
  page opens straight into the product.

## Suggested sequence

1. Seed more open finds — items 1, 4 and the jars all look better with 5 open finds than
   with 1, and that is a data change, not a UI change.
2. **P0** in order, then **P1**.
3. **L1**, the landing page — can run in parallel with P0, since it is a new file.
4. Deployment and the Ask agent — required by the rules. **L2/L3** are constraints on how
   that deployment is configured, not separate work.
5. P2 and README, then freeze **Aug 14**.
