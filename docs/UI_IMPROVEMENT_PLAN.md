# UI improvement plan

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

## Part 4 — Landing page and accounts

Requested 2026-07-28. This reverses a scope cut: `CLAUDE.md` lists accounts and
multi-tenancy under **Dropped — single demo tenant**. Recording that plainly, along with
the two risks, then the design.

### Two risks worth naming before building

**1. A registration wall sits between a judge and the demo.** Some fraction of judges
will not create an account to evaluate an entry, and the rules require setup
instructions a judge "can actually follow." Mitigation is item **L4** below: a
**one-click read-only demo session** with no registration, linked as prominently as the
signup button. The signup flow still exists and still gets demoed — it just is not the
only door.

**2. It competes with work the rules actually require.** Accounts are not a hackathon
requirement. Deployment, the Ask agent over the MCP server (the second CockroachDB tool
at runtime), and the README are. Roughly four days of work sits below against a **Aug 14
freeze**. Sequencing recommendation is at the end of this section.

The good news: **less new work than it sounds.** `business_id` is already threaded
through all nine tables with `ON DELETE CASCADE`, and both vector indexes are already
prefixed by it. The tenancy boundary exists — it just has no owner attached to it.

### L1 — The landing page

Routes:

| Path | Access |
|---|---|
| `/` | public — the landing page |
| `/signup`, `/login` | public |
| `/demo` | public — creates a read-only session on the seeded tenant |
| `/app` | requires a session |

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
4. **Two calls to action, equal weight** — *Create your account* and *See the live demo*.

Build it by extending `build_web.py` to emit `web/landing.html` from the same
`demo.json`. The landing page's numbers are then generated from the cluster, so it
**cannot drift from the product** — a marketing page that is structurally incapable of
overstating the record. Same cream/brass tokens, same Fraunces, same dotted texture. No
new visual language; this is a fourth page in the existing one.

### L2 — Schema

Two new tables, one new column. Additive, no migration of existing rows.

```sql
CREATE TABLE IF NOT EXISTS account (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email         STRING NOT NULL,          -- normalized (trimmed, lowercased) at the boundary
  password_hash STRING NOT NULL,          -- Argon2id, encoded form; never a raw digest
  created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE INDEX account_email_idx (email)
);

CREATE TABLE IF NOT EXISTS session (
  token_hash  BYTES PRIMARY KEY,          -- SHA-256 of the token; the token itself is never stored
  account_id  UUID NOT NULL REFERENCES account(id) ON DELETE CASCADE,
  read_only   BOOL NOT NULL DEFAULT false,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  expires_at  TIMESTAMPTZ NOT NULL,
  INDEX (account_id)
);

ALTER TABLE business ADD COLUMN IF NOT EXISTS account_id UUID REFERENCES account(id);
CREATE INDEX IF NOT EXISTS business_account_idx ON business (account_id);
```

Normalize email in Python rather than indexing `lower(email)` — it is testable offline
and does not depend on expression-index support. `clock_timestamp()` everywhere, per the
finding that `now()` is transaction-scoped in CockroachDB.

**Sessions live in CockroachDB.** The reflex would be DynamoDB or ElastiCache;
`CLAUDE.md` forbids a second store, and correctly — the session store is a genuine
second runtime use of the cluster, and it is a better disclosure answer than a
dev-time-only one.

### L3 — Auth mechanics

- **Argon2id** via `argon2-cffi`. Library defaults; do not hand-tune. Note that its 64MiB
  memory cost means the auth Lambda needs ≥512MB.
- **Opaque session tokens** — `secrets.token_urlsafe(32)`. Store `sha256(token)` as the
  primary key and look up by hash, so no secret comparison happens in application code.
- **Cookie**: `HttpOnly; Secure; SameSite=Lax; Path=/`. `SameSite=Lax` also blocks
  cross-site state-changing POSTs, which covers CSRF without a token scheme.
- **14-day absolute expiry.** No sliding refresh, no rotation — deferred, and say so.
- **Rate-limit login** — API Gateway throttling plus a per-account attempt counter.
- **Identical response and timing** for unknown-email and wrong-password. Never reveal
  which accounts exist.

**Deliberately not built, each because it opens a real surface:** email verification and
password reset (both need SES sender identity and a token flow), OAuth/social login,
MFA, and billing. Name these in the README as known gaps rather than leaving them
ambiguous.

### L4 — The judge path, and why `read_only` is a column

`/demo` mints a session bound to the seeded tenant with `read_only = true`. Because it is
a shared account, **read-only must be enforced in the repository layer, not the UI** —
otherwise one judge's clicking changes what the next judge sees. Any write path that
receives a read-only session raises before it reaches SQL.

### L5 — API Gateway authorizer

A REQUEST-type Lambda authorizer validates the session against CockroachDB and returns
the `business_id` and `read_only` flag in the authorizer context. Every downstream
handler scopes its queries by that `business_id` and never by one taken from the request
body.

### L6 — Tenant isolation is now the highest-risk code

The working agreement names the money math as highest-risk; **tenant isolation now
joins it.** Add to the shared contract suite, so both the in-memory and CockroachDB
implementations are held to it:

- every read scoped to business A returns zero rows belonging to business B
- vector search never crosses the tenant boundary, including when B's rows are the
  nearest neighbours
- a find and its evidence can never be written under a mismatched `business_id`
- a read-only session raises on every write path

The third case is the one to get right: `find_evidence` has no `business_id` of its own
and inherits it through `find_id`.

### Rules that must not be broken

- **`frontend/src/fixtures/demo.json` is committed to the repo.** `export_fixture.py`
  must never read `account` or `session`. A password hash reaching that file is a
  published credential.
- Never log tokens, hashes, or passwords — not at debug level, not in `agent_run`.
- The demo account's password is generated by a script and never printed, per the
  existing rule for the SQL password.
- Seed files carry no real email addresses.

### Effort and sequencing

| Item | Estimate |
|---|---|
| L1 landing page | ~1 day |
| L2 + L3 schema, hashing, sessions, tests | ~1.5 days |
| L5 authorizer and wiring | ~1 day |
| L4 + L6 read-only demo path and isolation tests | ~0.5 day |

**Recommendation:** build **L1 now** — it is cheap, it is the public face of the
submission, and it does not block anything. Hold **L2–L6 until deployment and the Ask
agent are done**, because those are required by the rules and accounts are not. If the
schedule tightens in the second week of August, ship the landing page with a waitlist
field instead of half-finished auth. A polished landing page plus a working demo beats a
login screen in front of an unfinished product.

---

## What is deliberately not in this plan

- **No redesign.** The visual language, the jar metaphor, the road, and the chat-first
  posture all stay exactly as they are — the landing page included.
- **No new palette.** The validated four-color set is already chosen; the Ledger just
  needs to use it.
- **No new framework.** Everything above is edits to the mock plus `build_web.py`. The
  React build stays unused.
- **No billing, and no multi-tenant onboarding beyond one business per account.** Signup
  creates exactly one business. Team members, roles, and invites stay cut.

## Suggested sequence

1. Seed more open finds — items 1, 4 and the jars all look better with 5 open finds than
   with 1, and that is a data change, not a UI change.
2. **P0** in order, then **P1**.
3. **L1**, the landing page — it can run in parallel with P0, since it is a new file.
4. Deployment and the Ask agent — required by the rules.
5. **L2–L6**, accounts — only once step 4 is done.
6. P2 and README, then freeze **Aug 14**.
