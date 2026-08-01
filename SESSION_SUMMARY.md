# SESSION SUMMARY

**Brass Tacks** — AWS + CockroachDB hackathon. Written 2026-07-28. Submission deadline **August 18**.

Repo: `github.com/a-kwo/aws-cockroach-hackathon` (currently **private** — must be public before submission).

---

## What the product is

A team of AI agents that works overnight for a small-business owner. Each night it reads
reviews, competitor menus and local trends; searches everything it has ever learned about
that business; and proposes one revenue move with a dollar figure attached. Weeks later it
checks whether that move actually paid and writes the verdict to a permanent ledger —
including the failures.

The reason this can win: it is **impossible without persistent memory**. The Meter judges
predictions made on earlier nights by agent runs that no longer exist. A stateless advisor
can give advice forever and never be wrong, because nothing it said was written down.

---

## Infrastructure — all verified working, not assumed

| Component | State |
|---|---|
| CockroachDB | Cluster `brasstacks`, **serverless on AWS us-east-1**, v26.2.1 |
| Schema | 9 tables applied; vector indexes live on `observation` and `business_fact` |
| Vector search | Proven on the real cluster: cosine index, correct nearest-neighbour ordering |
| Embeddings | Amazon Bedrock, Titan Text Embeddings V2, 1024 dims (matches `VECTOR(1024)`) |
| Reasoning | Anthropic API, `claude-opus-5` |
| AWS account | `881550374737`, region us-east-1 |

### The Bedrock constraint (important, and disclosed in the README)

AWS **cannot grant this account any current Claude model**. Verified rather than assumed:
`agreementAvailability: NOT_AVAILABLE` across us-east-1/us-east-2/us-west-2 while region,
entitlement and authorization are all green; the error directs to AWS Sales rather than
offering a use-case form; and the grant that did arrive was for two **retired** models that
return `ResourceNotFoundException`. 27 non-Anthropic Bedrock models invoke fine, so nothing
is wrong with Bedrock, the credentials or the region.

Consequence: reasoning runs on the Anthropic API, embeddings stay on Bedrock. Model calls
sit behind a provider interface, so a future grant is a config change.

**Silver lining:** the MCP connector is a first-party Anthropic API feature and is *not*
available on Bedrock. Being forced off Bedrock is what makes the planned Ask agent — which
queries the live cluster read-only over the CockroachDB Cloud MCP Server — possible at all.

---

## Backend — built, tested, running

**165 unit tests** (offline, no credentials needed) + **46 integration tests** (real cluster).

```
backend/src/brasstacks/
  config.py          typed settings; fails at startup with the variable named
  providers.py       Reasoner + Embedder protocols, real impls, offline fakes
  repository.py      memory-layer interface + in-memory implementation
  repository_pg.py   CockroachDB implementation — every SQL statement lives here
  finds.py           validates model output before it becomes a stored prediction
  meter.py           verdict logic (verified / estimated / miss)
  outcomes.py        where truth about outcomes comes from
  signals.py         where Radar's observations come from
  night.py           orchestrates one night; the local end-to-end harness
  agents/radar.py    observe -> embed -> dedup -> store
  agents/analyst.py  retrieve -> reason -> validated find + evidence
  agents/meter.py    read prior predictions -> judge -> ledger
```

### Design decisions worth preserving

- **One contract suite, two implementations.** The same 46 tests run against the in-memory
  fake and the live cluster. A fake that quietly diverges from real Postgres is worse than
  no fake, because agent tests would pass against a fiction.
- **`insert_find_with_evidence` is atomic.** A recommendation without the retrieved rows
  that produced it must be impossible to create. Enforced by the database, not by a comment.
- **The Meter never invents an outcome.** `NoOutcomeSource` is the default and yields an
  ESTIMATE, so the hit rate cannot be inflated by running the Meter more often.
- **A fresh find is `proposed`, never judgeable.** Only an owner decision makes it something
  the Meter will hold us to.
- **Money is integer cents everywhere.** Floats rejected at the boundary.

---

## The data in the cluster right now

```
127 observations · 15 owner facts · 3 owner rules · 17 finds
ledger: 6 verified · 1 miss · 4 measuring  ->  86% hit rate
$126.50/day earning now, against a $8,000/month goal
```

Seeded from `db/seed/*.json` and **backdated across eight weeks**, because verify windows
are ~14 days: seeding only "today" would leave the ledger with nothing but pending
predictions, and the ledger is the most important screen in the product.

Evidence similarity is **computed, not invented** — each find's title and move embedded as
the query the Analyst would have run, scored against each cited observation.

---

## Findings that changed the build

**1. Abstract queries retrieve badly.** Measured against the corpus:

| Query | Top similarity | Right cluster? |
|---|---|---|
| "Is there unmet demand we are not serving?" | 0.238 | No |
| "Should this restaurant open for lunch?" | **0.583** | Yes |

2.5x difference, and abstract queries surfaced the *wrong* observations. The Analyst
therefore issues **six concrete hypothesis queries** per night, not one open-ended one.
This is architectural, not prompt tuning.

**2. Similarity does NOT predict outcome.** An earlier claim of mine, now corrected:

| Verdict | Top evidence |
|---|---|
| verified | 0.702 |
| **miss** | **0.315** |
| verified | 0.157 |
| verified | **0.064** |

Two verified wins have weaker evidence than the published miss. The UI must never imply
that thin evidence explains a failure. Also: 46% of all 108 evidence rows sit below 0.30,
which killed a proposed "ink density = confidence" encoding — most of the product's
receipts would have rendered at ~2:1 contrast.

**3. Live web search is harmful for this demo.** The tenant is fictional, so searching for
it returned trade-show videos and years-old market reports — 40 of 40 signals irrelevant,
polluting memory and dragging third-party trademarks into a demo where the rules bar them.
Now opt-in via `--web`. Against a real business the same source is useful.

**4. The Analyst had no memory of its own recommendations.** It proposed the same waitlist
find on nights 1 and 3. It now sees recent finds and is told not to repeat them.

**5. `now()` is the transaction timestamp in CockroachDB.** Rows written in one transaction
tied on `created_at`. Caught by the contract suite — the in-memory fake used a monotonic
clock and hid it. All audit timestamps now use `clock_timestamp()`.

**6. Models double-escape their own JSON.** A find carried the literal characters `—`
rather than an em dash and rendered as garbage. Repaired at the trust boundary in
`parse_find`. Found only by rendering the app and looking at it.

**7. The original verified/miss colours failed colourblindness.** `#177A4C` / `#B0452F`
scored ΔE 6.0 under deuteranopia (~8% of men) on the most important distinction in the
product. Validated replacement: `#118066` / `#C0442A` / `#2E5EA8` / `#A8781F` at ΔE 10.0.
**This flaw is still present in the pitch deck and should be fixed there too.**

---

## Frontend — three redesigns rejected, mock adopted

Three attempts (single-page, tabbed, and a full spec-driven rebuild called THE NIGHT DESK)
were each judged inferior to the original `Product Demo/brasstacks-jar-demo.html`.

Seeing the mock rendered finally explained why: **it is a conversation with an agent, where
the rebuilds were documents about one.** The nav is the loop with live state in each pill,
the road to the goal is an illustrated road rather than a bar chart, and its growth chart
answers the two-months-of-data problem better than the rebuild's principled refusal — one
solid bar plus ghosted dashed projections, honest and forward-looking at once.

**Current approach:** `scripts/build_web.py` treats the mock as a template and splices in
live cluster values, leaving its design untouched. Output is `web/index.html` (gitignored;
regenerate with the script).

Already live in it: coins from undecided finds, road stops with evidence counts,
earning-now, goal and shortfall, review count, and a footer naming the corpus size and
verified record.

Still the mock's scripted values: **the growth chart bars, "72% mapped", and the opening
chat message.**

The React build remains in `frontend/` but is **unused**. Its one piece worth keeping is the
long-prose logic — the mock was written for one-line finds and real Analyst output runs to
twelve lines in four distinct shapes.

`docs/DESIGN_SPEC.md` (90k chars) is the output of a 9-agent design workflow. Superseded as
a build target, but sections 1, 3 and 7 contain the data-driven reasoning above and are
worth keeping.

---

## How to run it

```bash
# tests
pytest                          # 165 unit, offline, no credentials
pytest -m integration           # 46 against the live cluster

# a night of the loop
python -m brasstacks.night --nights 3 --step 10 --accept-proposals

# refresh the frontend from the cluster
python scripts/export_fixture.py
python scripts/build_web.py
cd web && python -m http.server 8900
```

Setup gotchas that will otherwise cost a judge time, and belong in the README:

- `sslmode=verify-full` needs the cluster CA file; download from
  `https://cockroachlabs.cloud/clusters/<cluster-id>/cert`
- `sslrootcert=system` **fails on Windows** — psycopg's bundled libpq does not resolve it
  to the Windows trust store
- Edit `.env` with `python scripts/env_file.py set KEY value`, never by hand. Hand-editing
  destroyed the connection string once, including a generated password that existed nowhere
  else. `python scripts/provision_db_user.py` rotates and rebuilds it if that happens again.
- CLI output needs `PYTHONIOENCODING=utf-8` on Windows — every find carries an emoji

---

## Not done yet

**Frontend**
- Growth chart, "72% mapped", and chat greeting still on scripted values
- The demo tenant is too settled: 1 undecided find and 1 saved, so the jars look empty and
  the road has 3 stops instead of 5. Seed more open finds — this is a data problem, not UI.
- Mobile unverified

**Backend / infra**
- Nothing deployed to AWS yet: no Lambda, EventBridge, S3, API Gateway, no IaC
- The Ask agent over the CockroachDB MCP Server — the second required Cockroach tool at
  runtime — is designed but not built
- `ccloud` setup automation exists ad hoc; not yet a committed reproducible script

**Submission**
- README, architecture diagram, setup instructions
- CockroachDB tool disclosure (Vector Indexing, MCP Server, ccloud CLI) and AWS service
  disclosure (Bedrock, Lambda, EventBridge, S3, API Gateway)
- Provenance disclosure: `Product Demo/` predates this project and was built for an
  abandoned competition
- Strip all Gemini / XPRIZE branding from the deck
- Flip the repo public and confirm GitHub detects the MIT licence
- Demo video under 3 minutes, showing the memory layer working
- **Still on root AWS credentials** — swap to an IAM user before writing deploy code

---

## Suggested order from here

1. Seed more undecided finds so the demo has something to show
2. Wire the remaining scripted values in the mock to real data
3. Deploy: container-image Lambdas (psycopg needs manylinux wheels), EventBridge, API Gateway
4. Ask agent over MCP — the second CockroachDB tool
5. README and disclosures
6. **Feature freeze Aug 14**, record the video, submit Aug 17

Projects lose by building until the deadline and never recording.

---

## 2026-08-01 — decision pipeline consistency fix

- For You decisions now project over the build-time model before Memory Engine derives its stages.
- **Pass** changes the decision gate from 5 waiting to 4 waiting and increments **passed**; Maker remains at 0 because passed work stops at the decision gate.
- **Do it** changes the decision gate from 5 waiting to 4 waiting and adds one accepted, undrafted item to Maker.
- Replaced the ambiguous Owner Decision metric **saved** with **approved** and **passed**. The separate `later` state is shown only as **Saved for later** in the expanded routing details.
- Connected builds keep successful decision receipts across reloads. The unconnected demo still starts clean.
- Full offline suite at that milestone: **367 passed, 58 deselected**.


---

## 2026-08-01 — live workflow freshness (v6)

- Added a read-only `GET /workflow` Lambda/API Gateway route backed directly by CockroachDB.
- The static export remains the instant, failure-tolerant first paint; the live response overlays current owner workflow state without rebuilding the site.
- One startup sync keeps For You aligned with decisions made on another device. Memory Engine then revalidates every 15 seconds only while operators can see it.
- Current decisions, agent runs, token receipts, Maker artifacts, and Meter verdicts update in place. A failed refresh keeps the last good state and marks it stale.
- Conditional `ETag` requests return `304 Not Modified` when the workspace has not changed.
- The workflow read is SQL-only and consumes **zero model tokens**. It returns cited evidence, not embeddings or the full observation corpus.
- Tenant access is an SSM-configured business allowlist; request parameters cannot enumerate arbitrary businesses.
- Lifecycle arrays are now re-derived from authoritative rows, so Later, accepted work, Maker queues, and Meter windows cannot drift from the displayed find status.
- Full offline suite: **386 passed, 58 deselected**. Browser smoke test verified cross-device Pass + Do it, Maker queue routing, and conditional ETag refresh.

---

## 2026-08-01 — Radar statistics clarified (v7)

- Replaced the ambiguous Radar labels **memories** and **signal types** with **market signals stored**, **market channels**, and **newest signal**.
- The compact owner row now says what Radar actually has and shows the last scan date instead of repeating the same total twice.
- Expanded Radar groups raw database kinds into operator-friendly channels: Reviews, Competitors, Demand, and optional Owner context/Other.
- Added an explicit explanation that the number is the total evidence currently stored for the owner, not new recommendations or signals added in the latest scan.
- Added scan freshness and receipt details: last Radar scan, scan duration, newest stored signal, and the recorded run note/status.
- Full offline suite: **387 passed, 58 deselected**.


---

## 2026-08-01 — Analyst traceability (v8)

- Replaced the ambiguous **6 searches / run** label with an operator-readable Analyst trace.
- The expanded stage now shows: signals searchable, six market questions, configured or
  recorded candidate matches, unique context sent to the model, and evidence rows saved.
- Clarified that these are CockroachDB vector-memory searches, not six internet searches.
- Every waiting recommendation expands to its rationale, recommended move, exact cited
  `find_evidence` rows, and its linked Analyst run receipt when present.
- Added a compact versioned `analyst_trace_v1` receipt to `agent_run.note`, recording per-query
  hit counts, raw matches, unique matches after deduplication, cited rows, and output find id.
- Linked `find.run_id` into both the static build and live workflow projection so the operator
  view uses the exact run that created a recommendation rather than an unrelated latest run.
- Input/output token usage remains stored on the same `agent_run`. Historical imported finds
  with no run row now say **No receipt** and explicitly explain that this is not zero tokens.
- Browser smoke test verified Memory Engine rendering and the expandable Analyst workflow.
- The structured receipt also stores the exact query text and per-query retrieval limit, and
  the workflow read keeps run rows referenced by open finds even when they are older than the
  normal per-agent activity window.
- Evidence rows now carry observation id, rank, source name, subject, date, and similarity for
  a complete recommendation-to-memory audit trail.
- Full offline suite: **396 passed, 58 deselected**.

---

## 2026-08-01 — Owner decision audit trail (v10)

- Replaced the Owner Decision stage's aggregate-only routing list with a newest-first,
  expandable decision history.
- Every recorded **Do it** or **Pass** now shows the recommendation, owner account, local
  decision time, and downstream route in the collapsed row.
- Expanding a decision reveals the decision, actor account, exact timestamp, routing,
  receipt source, and full CockroachDB `find` identifier.
- The static build now preserves `find.decided_at`; the live workflow endpoint already
  supplies the same field. Historical rows without a receipt say **Time not recorded**
  rather than inventing one.
- The decision API now generates one UTC server timestamp, writes it to CockroachDB, and
  returns that same value to the browser so the audit trail does not depend on a skewed
  client clock.
- Unconnected demo decisions are visibly labelled **This browser · demo only**. Connected
  decisions are labelled as CockroachDB records/live workflow reads.
- The compact metrics now use the owner's actual button language: **waiting**, **Do it**,
  and **Pass**.
- Current actor identity is the business-owner account inferred from the owner workspace.
  A named human identity will require the planned API Gateway/JWT authentication layer.
- Full offline suite: **401 passed, 58 deselected**. Browser smoke test verified Pass,
  timestamp rendering, route visibility, and expandable record details.

---

## 2026-08-01 — Memory Engine visual operations mode (v12)

- Removed the redundant header subtitle and the ambiguous global **Need attention** KPI. Attention remains attached to the exact owner handoff and agent stage where an operator can act.
- Added an accessible two-way **Operations / Live graph** toggle. Both modes consume the same owner-scoped workspace model and live `/workflow` refresh.
- Added a multi-owner selector. Every business remains isolated by `business_id`; selecting an owner redraws the five-agent pipeline, stage inspector, market-memory chart, decision chart, and outcome ledger for that owner.
- Added an animated five-stage flow map for Radar → Analyst → Owner decision → Maker → Meter, including status pulses, moving handoff particles, animated bars, and a reduced-motion fallback.
- Added live, data-derived charts for portfolio handoffs, owner decisions, market memory, and the outcome ledger. No presentation-only counts are introduced.
- Each graph node opens a stage inspector; **Open detailed trace** returns to Operations mode and expands the exact owner and agent receipt.
- Validated at desktop, tablet, and mobile widths with no page-level horizontal overflow.
- Offline suite: 410 passed, 58 cloud integration tests deselected.

---

## 2026-08-01 — CockroachDB memory and token-efficiency proof (v13)

- Added an above-the-fold **CockroachDB memory advantage** panel to both Memory Engine modes.
- The selected owner now has a visible retrieval funnel: persistent memories → bounded candidate matches → deduplicated model context → evidence saved with the recommendation.
- Added owner-scoped KPIs for persistent memories, context kept out, actual Analyst model tokens, and the SQL-only workflow refresh that uses **0 LLM tokens**.
- Added an animated token-efficiency chart to Live graph mode, derived from the same owner workspace as the operations trace.
- Actual input/output token usage comes only from the linked `agent_run` provider receipt. Historical imports without that receipt say **Pending live run** and explicitly state that the value is not zero.
- Context reduction remains labelled as a row-based retrieval metric and is never presented as an invented provider-token saving.
- The live `/workflow` read is identified separately as a zero-model-token CockroachDB status projection.
- Validated Operations and Live graph modes at 1920, 1440, 1024, 768, and 390 px with no horizontal overflow or browser console errors.
- Offline suite: **414 passed, 58 cloud integration tests deselected**.

## 2026-08-01 — Portfolio-scoped memory efficiency KPIs (v14)

The Memory Engine headline KPI strip now aggregates across every owner workspace rather
than changing with the currently selected business. `Context kept out` is calculated as a
weighted portfolio reduction: total owner-scoped context bounds divided by total persistent
memories. The card is explicitly labelled `All owners`. Analyst token usage likewise sums
the latest recorded owner receipts and states receipt coverage. Detailed retrieval funnels
remain owner-scoped and are labelled `Selected owner`.

---

## 2026-08-01 onboarding update

The landing page now uses **Sign up** and routes to `web/signup/`. A two-step workspace
setup captures the minimum owner-scoped agent brief: owner identity, business/category,
market, optional known URL, buyer segments, core offers, channels, and one priority. The
right-hand preview explains how the profile narrows Radar and Analyst.

The public/local build stores this profile under `brass-tacks-onboarding-profile-v1` and
opens an honest first-run workspace with zero inherited signals, recommendations, revenue,
or outcomes. A configured `ONBOARDING_API_ENDPOINT` can accept the same structured payload,
but secure authentication and production tenant provisioning remain future work and must not
be implied by the demo.
