# Brass Tacks

**An agent with a track record.** A team of AI agents works overnight for a small
business: it reads the reviews, the rivals and the local trends, proposes one revenue
move with a dollar figure attached, and then — weeks later — checks whether that move
actually paid. Verdicts go on a permanent ledger, **including the failures**.

Built for the AWS + CockroachDB hackathon.

**Live demo: [trybrasstacks.com](https://trybrasstacks.com)** The
fastest way in is the **guided interactive demo**: press *Try the interactive
demo* on the landing page. 

---

## Try it in the browser — nothing to install

The demo walks through onboarding on a sample workspace, plays a sample first night, then tours
the morning board and the operator console — the captions name each CockroachDB
and AWS piece as it appears on screen. The tour runs against committed sample
data, so nothing a visitor clicks can touch a real tenant's ledger.

Worth seeing on the way through:

1. **Onboarding** — the minimum agent brief: who owns the account, what the
   business sells, where it competes, its buyer segments, sales channels, and the
   first outcome to optimize. The live brief on the right shows how those answers
   narrow Radar and Analyst before any recommendation is allowed to appear. On
   the real site the same form posts to the deployed onboarding API and creates
   a workspace whose facts the Analyst reads; a workspace starts honestly at
   zero signals until its first night runs.
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
4. **Growth** — the deployed tenant's moves are all still inside their measurement
   windows (the earliest verdict comes due 2026-09-01), and the page says so rather
   than inventing a track record: predictions are labelled as forecasts, a modelled
   figure is never labelled *Actual*, and nothing the owner clicks can raise the
   verified number. The judged states — verified, modelled, and a deliberate miss —
   are demonstrated by the fictional seed tenant (see Disclosures).

The operator renderer accepts an `ownerWorkspaces` array for a multi-owner portfolio. The
committed demo fixture contains one real (identity-stripped) owner account, so the UI does
not fabricate extra tenants merely to make the console look busy.

### Run the site locally instead (optional, no credentials)

The demo data is committed, so the same pages render with no AWS keys and no
database:

```bash
python scripts/build_web.py       # renders web/ from the committed fixture
cd web && python -m http.server 8901
```

Then open **http://127.0.0.1:8901** — landing page, onboarding at `/signup/`,
and the dashboard at `/app/`.


### New owner onboarding

The landing page now routes **Sign up** to `/signup/`. Onboarding is intentionally short and
creates one structured, tenant-scoped agent brief rather than a long questionnaire:

- owner name and work email
- business name, category, primary market, and optional known website/review page
- up to three buyer segments and three core offers
- current sales channels and one ranking priority
- the standing rule that every recommendation still requires owner approval

On the deployed site the form posts to the onboarding API (injected at build time
as `ONBOARDING_API_ENDPOINT`): it creates the owner account and workspace, and
the answers become the `business_fact` rows the Analyst consumes.
A new workspace switches the app to a true first-run state: no inherited
recommendations, no inherited revenue, no fabricated market memory. Radar is shown
as **Ready to scan**, Analyst as idle, and the next handoff is the first
owner-scoped market sweep. A build with no endpoint configured (such as the local
fixture build above) falls back to browser-only demo mode: the profile is stored
under `brass-tacks-onboarding-profile-v1` and nothing persists server-side. With
`?tour=` the page walks itself with sample answers and persists nothing at all.


## Where things live

```
site/                frontend source — landing.html, signup.html and app.html
scripts/build_web.py splices the model into all three, writes web/
web/                 build output (gitignored — never edit, always regenerate)

backend/src/brasstacks/
  agents/radar.py    observe -> embed -> dedup -> store
  agents/analyst.py  retrieve -> reason -> a validated find with its evidence
  analyst_trace.py   structured query/retrieval/token receipt in agent_run.note
  provenance.py      which retrieved rows are one page or one storefront
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
  meter.py           verdict logic and the reported-amount conversion
  outcomes.py        where a measurement comes from; finds.py validates model output

deploy/              SAM, Dockerfile, Step Functions ASL and deployment runbook

db/schema.sql        30 tables, including decision_event/work_task/task_event/tool_execution
                     and find_outcome, the owner's measured results
db/fixtures/         the identity-stripped export of the live demo tenant the site build reads
db/seed/             the fictional, hand-written Rosa's Trattoria corpus scripts/seed.py plants

docs/MULTI_TENANT_AGENT_PLATFORM.md
                     scale, task, tool, security and rollout plan
docs/RECONSIDER_DECISION_CYCLES.md
                     append-only redo policy, cancellation rules and acceptance test
docs/MAKER_REVIEW_DELIVERY_REVISION.md
                     structured Maker review, SES delivery telemetry and chat revisions

PRODUCT.md           who this is for and what must never be fabricated
DESIGN.md            the visual system, with the rules and the reasons
```

## Running it for real

Needs **Python 3.12+**, a CockroachDB Cloud cluster, AWS credentials for Bedrock
embeddings, and an Anthropic API key. Install the backend package first — it
declares every runtime dependency (`psycopg`, `anthropic`, `boto3`, `httpx`,
`certifi`) plus pytest:

```bash
python -m pip install -e "backend[dev]"
```

Then copy `.env.example` to `.env` and fill it in — the example file documents
every value, including the ones that are safe to leave blank.

```bash
python db/migrate.py                       # apply the schema
python scripts/seed.py --reset             # ~150 embedding calls, a couple of minutes
python -m brasstacks.night --nights 3      # run the loop
python scripts/export_fixture.py           # refresh db/fixtures/demo.json
python scripts/build_web.py                # rebuild the site
```

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
3. **Model context** — the deduplicated rows that actually enter the Analyst prompt, at
   most two from any one source so a single long page cannot fill it. Each row carries its
   host and the time it was observed, to the second.
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

## Editable owner profiles

Signed-in owners can open the three-line menu in the app header to view and edit
the contact and business facts Brass Tacks uses. Operators see the recorded email
for every owner workspace in Memory Engine. See
[`docs/OWNER_PROFILE.md`](docs/OWNER_PROFILE.md) for storage, privacy, legacy
email backfill, and Maker-recipient routing details.

## Licence

MIT. See [`LICENSE`](LICENSE).
