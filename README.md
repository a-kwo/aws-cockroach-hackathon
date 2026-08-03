# Brass Tacks

**An agent with a track record.** A team of AI agents works overnight for a small
business: it reads the reviews, the rivals and the local trends, proposes one revenue
move with a dollar figure attached, and then — weeks later — checks whether that move
actually paid. Verdicts go on a permanent ledger, **including the failures**.

Built for the AWS + CockroachDB hackathon.

The production-shaped multi-user execution design is documented in
[`docs/MULTI_TENANT_AGENT_PLATFORM.md`](docs/MULTI_TENANT_AGENT_PLATFORM.md). It
separates agent reasoning from durable tasks, approval, idempotency, and external
tools. The append-only redo/reconsider policy is documented in
[`docs/RECONSIDER_DECISION_CYCLES.md`](docs/RECONSIDER_DECISION_CYCLES.md).

The claim this rests on: the Meter judges predictions made on earlier nights by agent
runs that no longer exist. A stateless advisor can give advice forever and never be
wrong, because nothing it said was written down. This writes it down first.

---

## See it in 30 seconds, with no credentials

The demo data is committed, so you do not need AWS keys or a database to look at the
product:

```bash
python scripts/build_web.py       # renders web/ from the committed fixture
cd web && python -m http.server 8901
```

Then open **http://127.0.0.1:8901** — landing page, onboarding at `/signup/`,
and the dashboard at `/app/`.

Worth clicking, in this order:

1. **Sign up** — create a new owner workspace in about two minutes. The minimum agent
   brief captures who owns the account, what the business sells, where it competes, its
   primary buyer segments, sales channels, and the first outcome to optimize. The right
   side previews how those answers narrow Radar and Analyst before any recommendation is
   allowed to appear. In the public build the profile is stored only in the browser and
   the resulting workspace starts honestly at zero signals; a configured, authenticated
   `ONBOARDING_API_ENDPOINT` can persist the same payload later.
2. **For You** — inspect a recommendation, then choose **Do it** or **Pass**. The
   card immediately locks into **Saving**, then shows the recorded decision so repeated clicks
   are not ambiguous. With the live API configured, CockroachDB commits the decision and creates
   or reuses one idempotent Maker task before SQS delivery. **Do it** enters the durable task
   workflow; **Pass** records the choice without entering Maker. A later **Undo Pass** uses the
   same task contract rather than creating duplicate work. After **Do it**, the owner may
   choose **Return to For You** from the recommendation chat or Memory Engine. That opens a
   new decision cycle without deleting the original approval, task, draft, or email receipt.
3. **Memory Engine** — scan the owner-by-stage pipeline: one row per business owner and
   one column for Radar, Analyst, the decision gate, Maker, and Meter. The highlighted cell
   is the next handoff. Expand an owner row, then open only the agent or CockroachDB receipt
   you need. Radar reports **market signals stored**, not recommendations or signals added
   today; its expanded view groups the raw rows into Reviews, Competitors, and Demand and
   shows the last scan plus the newest stored signal. Analyst exposes the full narrowing path:
   stored signals → six concrete vector-memory questions → raw matches → unique context →
   cited evidence. Every output can be expanded to its rationale, action, exact
   `find_evidence` rows, and linked `agent_run` receipt. The Owner decision stage also keeps a
   newest-first decision receipt: recommendation, **Do it** or **Pass**, business-owner actor,
   local display time, routing outcome, persistence source, and the full CockroachDB find ID.
   A historical recommendation with no run row says **No receipt** rather than pretending the
   missing token count is zero. In a connected build, the status chip turns **Live** and the
   matrix revalidates against CockroachDB while this tab is visible.
4. **Growth** — every judged move remains visible, including the espresso miss: predicted
   +$12.00/day, actual $0.00. Verified money never mixes with projected money.

The operator renderer accepts an `ownerWorkspaces` array for a multi-owner portfolio. The
committed demo fixture contains one fictional owner account, so the UI does not fabricate
extra tenants merely to make the console look busy.


### New owner onboarding

The landing page now routes **Sign up** to `/signup/`. Onboarding is intentionally short and
creates one structured, tenant-scoped agent brief rather than a long questionnaire:

- owner name and work email
- business name, category, primary market, and optional known website/review page
- up to three buyer segments and three core offers
- current sales channels and one ranking priority
- the standing rule that every recommendation still requires owner approval

The app reads the profile under `brass-tacks-onboarding-profile-v1` and switches to a true
first-run state: no Rosa recommendations, no inherited revenue, no fabricated market memory.
Radar is shown as **Ready to scan**, Analyst as idle, and the next handoff is the first
owner-scoped market sweep. This local mode exists so the UX can be tested without creating an
unsafe public tenant-provisioning endpoint. A future authenticated endpoint can be injected at
build time with `ONBOARDING_API_ENDPOINT`; the signup payload already matches the business
facts and owner rules the Analyst consumes.

## Where things live

```
site/                frontend source — landing.html, signup.html and app.html
scripts/build_web.py splices the model into all three, writes web/
web/                 build output (gitignored — never edit, always regenerate)

backend/src/brasstacks/
  agents/radar.py    observe -> embed -> dedup -> store
  agents/analyst.py  retrieve -> reason -> a validated find with its evidence
  analyst_trace.py   structured query/retrieval/token receipt in agent_run.note
  agents/maker.py    create one owner-ready draft after an atomic task claim
  agents/meter.py    read prior predictions -> judge -> the ledger
  agents/ask.py      answer the owner by querying the cluster over MCP
  handlers/          HTTP, queue, workflow-worker, email-tool and scheduler entry points
  decisions.py       append-only owner-decision events and reconsider policy
  decision_schema.py runtime bootstrap for decision cycles and event history
  tasks.py           durable task states, cycle-aware idempotency and FIFO resource keys
  maker_dispatch.py  commit-first task creation and SQS FIFO dispatch
  tools/              constrained external tools; SES review email is the first
  repository_pg.py   transactional SQL, atomic claims and execution receipts
  workflow_snapshot.py read-only operator projection for the live dashboard
  meter.py           verdict logic; finds.py validates model output

deploy/              SAM, Dockerfile, Step Functions ASL and deployment runbook

db/schema.sql        15 tables, including decision_event/work_task/task_event/tool_execution
db/fixtures/         the exported demo tenant the site build reads

docs/MULTI_TENANT_AGENT_PLATFORM.md
                     scale, task, tool, security and rollout plan
docs/RECONSIDER_DECISION_CYCLES.md
                     append-only redo policy, cancellation rules and acceptance test

PRODUCT.md           who this is for and what must never be fabricated
DESIGN.md            the visual system, with the rules and the reasons
```

## Running it for real

Needs a CockroachDB Cloud cluster, AWS credentials for Bedrock embeddings, and an
Anthropic API key. Copy `.env.example` to `.env` and fill it in.

```bash
python db/migrate.py                       # apply the schema
python scripts/seed.py --reset             # ~150 embedding calls, a couple of minutes
python -m brasstacks.night --nights 3      # run the loop
python scripts/export_fixture.py           # refresh db/fixtures/demo.json
python scripts/build_web.py                # rebuild the site
```

### Durable multi-tenant task execution

An approved recommendation is no longer interpreted as “invoke Maker and hope.” The request
commits one `work_task` in CockroachDB, SQS FIFO buffers the dispatch, Step Functions Standard
orchestrates the attempt, and the Maker worker must atomically claim the row before it constructs
the model client. Duplicate deliveries therefore exit before spending tokens or generating a
second draft. A five-minute SQL-only reconciler recovers missed messages and expired leases.

An accepted recommendation can be returned to For You safely. Brass Tacks appends a
`owner.reopened` event, supersedes the old task and artifact, increments the decision cycle,
and leaves the earlier Do it and tool receipts intact. A later approval creates a new
cycle-aware Maker task. Customer-facing actions and Meter results cannot be erased; they
require a corrective task or recommendation revision.

The first constrained execution tool sends a completed draft to a configured review inbox through
Amazon SES. It is disabled by default and never accepts a model-chosen recipient. The full design,
current implementation boundary, SES test flow, and AgentCore/OAuth/browser roadmap are in
[`docs/MULTI_TENANT_AGENT_PLATFORM.md`](docs/MULTI_TENANT_AGENT_PLATFORM.md).

## Tests

```bash
python -m pytest backend/tests -q      # 681 offline tests in this version
python -m pytest -m integration -q      # 65 cloud/live tests when configured
```

The unit suite must stay green with no cloud account. `backend/tests/test_site_build.py`
asserts the honesty invariants: only verified money reaches the headline figure, at most
one projected month, an estimate is never labelled "Actual", and a miss always survives
into the view model.


### Live workflow freshness

The site is still rendered from a CockroachDB export so it has an immediate, resilient first
paint. In a connected build, that snapshot is no longer the operator view's ceiling:

- `Do it` / `Pass` writes through the Decision API and is projected into the UI immediately.
- The app performs one read-only Workflow API sync at startup, then Memory Engine revalidates
  every 15 seconds while its tab remains visible.
- Decisions from another device, immutable decision-cycle events, durable task states and
  events, tool receipts, current and superseded Maker artifacts, current agent runs, and Meter
  verdicts are merged into the operator matrix without
  rebuilding the site.
- Conditional `ETag` requests return `304` when nothing changed; polling pauses when the tab is
  hidden or the operator leaves Memory Engine.
- If the endpoint is down, the last good live state remains visible as stale. Before the first
  live response, the build snapshot remains the honest fallback.

The endpoint returns the compact workflow receipt, not observation embeddings or the full
corpus. It is a SQL-only read path and consumes zero model tokens. Tenant access comes from a
configured business allowlist rather than a request parameter.

### Memory Engine operator views

Memory Engine has two synchronized operator views:

- **Operations** is the traceable owner-by-stage matrix with expandable run, evidence, decision,
  artifact, and outcome receipts.
- **Live graph** is the visual portfolio view. Select an owner to redraw the animated
  Radar → Analyst → Owner decision → Maker → Meter flow and its owner-scoped charts.

Both views consume the same `normalizeOwnerWorkspaces()` projection. When `/workflow` is
connected, the selected owner, agent status, portfolio handoff load, market-memory mix, owner
decisions, and outcome ledger refresh from CockroachDB. Without the endpoint, the UI labels the
data honestly as a build snapshot. Motion is disabled for `prefers-reduced-motion`.

### CockroachDB memory and token-efficiency proof

Memory Engine keeps the competition evidence above the fold for the selected owner:

1. **Persistent memory** — the number of owner-scoped observations searchable in CockroachDB.
2. **Candidate retrieval** — six concrete vector questions, each with a bounded result set.
3. **Model context** — the deduplicated rows that actually enter the Analyst prompt.
4. **Cited evidence** — the smaller set persisted with the recommendation for auditability.
5. **Actual model usage** — provider-reported input and output tokens stored on the linked
   `agent_run` row.
6. **Zero-token operations read** — `/workflow` projects current state directly from
   CockroachDB through SQL and invokes no LLM.

The UI deliberately labels context reduction as **row-based**. It never converts a memory-row
percentage into invented token savings. Once a live Analyst run has a linked receipt, the same
panel shows the actual provider input/output tokens; an older fixture with no receipt says
**Pending live run**, not zero. Every graph and KPI remains owner-scoped, so a multi-tenant
operator can compare efficiency without mixing one business's memory with another's.

## Setup gotchas that will otherwise cost you an hour

- `sslmode=verify-full` needs the cluster CA. Download it from
  `https://cockroachlabs.cloud/clusters/<cluster-id>/cert`.
- **`sslrootcert=system` fails on Windows** — psycopg's bundled libpq does not resolve it
  to the Windows trust store. Point at the downloaded file instead.
- Edit `.env` with `python scripts/env_file.py set KEY value`, never by hand.
- CLI output needs `PYTHONIOENCODING=utf-8` on Windows; every find carries an emoji.

## Disclosures

**CockroachDB is the memory layer, and it is load-bearing.**

| Tool | What the agent actually does with it |
|---|---|
| Distributed Vector Indexing | Radar embeds every observation; the Analyst runs six semantic searches over the whole corpus before proposing anything. `VECTOR(1024)` with a `business_id` prefix. |
| Cloud Managed MCP Server | The **Ask** agent answers owner questions by running SQL against the live cluster read-only over the managed MCP server. Read-only is enforced twice (no write consent, scoped Cloud RBAC), every turn writes an `agent_run` row carrying the executed SQL, and the prompt forbids answering from the model's own knowledge — an answer that touched no tools is recorded as such. |
| ccloud CLI | Cluster provisioning, SQL user creation, network config. |

**AWS**

| Service | Role |
|---|---|
| Bedrock | Titan Text Embeddings V2 generates every vector in the index. No embeddings, no retrieval, no memory. |
| Lambda | Container-image API handlers, queue bridge, atomic Maker worker, email tool, reconciler and scheduled agents. |
| SQS FIFO | Buffers approved work, orders only conflicting resources, deduplicates one dispatch attempt, and retains exhausted messages in a DLQ. |
| Step Functions Standard | Runs one durable Maker workflow attempt and records the orchestration receipt. CockroachDB remains the task source of truth. |
| EventBridge Scheduler | Fires the nightly intelligence loop and the SQL-only Maker reconciliation safety net. |
| S3 | Stores complete Maker artifacts separately from the public site bucket. |
| SES | Optional first execution tool: email one completed draft to a server-configured owner/test inbox. |
| API Gateway | Throttled HTTP routes for Ask, decisions, authentication, onboarding and live workflow state. |

Deployed with AWS SAM — see `deploy/` for the template and the runbook.

**Reasoning runs on the Anthropic API, not Bedrock.** This is forced, not preferred.
AWS could not grant this account any current Claude model: `agreementAvailability` is
`NOT_AVAILABLE` across three regions while region, entitlement and authorization are all
green, and the grant that did arrive was for two retired models that return
`ResourceNotFoundException`. 27 non-Anthropic Bedrock models invoke fine, so nothing is
wrong with Bedrock, the credentials or the region. Embeddings stay on Bedrock, so
retrieval is AWS end to end. Model calls sit behind a provider interface, so a future
grant is a config change.

**Rosa's Trattoria is a fiction.** The corpus is hand-written so the demo is
reproducible and carries nobody's real reviews. Every number rendered by the site is
queried out of the cluster, not typed into the markup. There are no real customers,
testimonials, pricing, benchmarks, peer data or P&L, and `PRODUCT.md` records those
absences so future work does not invent them.

## Provenance

`Product Demo/` held a pitch deck and a clickable front-end mock built **before this
project began**, for a different competition that was abandoned. The mock was generated
with AI and then refined by hand. The directory has since been **removed from the working
tree**; it remains in git history, and removing it does not retract anything below.

**The shipped frontend descends from that mock.** After three redesigns were tried and
rejected, `Product Demo/brasstacks-jar-demo.html` was copied to `site/app.html` and
rebuilt from there: its data now comes from CockroachDB, its invented panels were
replaced, and a Ledger screen was added — but its layout, CSS and interaction model
are descended from that file. It was never a build input; the build reads `site/`.

An abandoned React rebuild ("The Night Desk") lived at `frontend/` and was **deleted** —
it was the third rejected redesign and only caused confusion about which directory was
the product. It remains in git history.

## Licence

MIT. See `LICENSE`.

## Editable owner profiles

Signed-in owners can open the three-line menu in the app header to view and edit
the contact and business facts Brass Tacks uses. Operators see the recorded email
for every owner workspace in Memory Engine. See
[`docs/OWNER_PROFILE.md`](docs/OWNER_PROFILE.md) for storage, privacy, legacy
email backfill, and Maker-recipient routing details.
