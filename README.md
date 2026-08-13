# Brass Tacks

**An agent with a track record.** A team of AI agents works overnight for a small
business: it reads the reviews, the rivals and the local trends, proposes one revenue
move with a dollar figure attached, and then — weeks later — checks whether that move
actually paid. Verdicts go on a permanent ledger, **including the failures**.

Built for the AWS + CockroachDB hackathon.

**Live demo: [trybrasstacks.com](https://trybrasstacks.com)** — the landing page, with
the owner dashboard at [/app/](https://trybrasstacks.com/app/) and onboarding at
[/signup/](https://trybrasstacks.com/signup/). Served by the deployed stack described
below; the guided tour on the dashboard runs against committed sample data, so nothing
a visitor clicks can touch a real tenant's ledger.

The production-shaped multi-user execution design is documented in
[`docs/MULTI_TENANT_AGENT_PLATFORM.md`](docs/MULTI_TENANT_AGENT_PLATFORM.md). It
separates agent reasoning from durable tasks, approval, idempotency, and external
tools. The append-only redo/reconsider policy is documented in
[`docs/RECONSIDER_DECISION_CYCLES.md`](docs/RECONSIDER_DECISION_CYCLES.md). The professional Maker review, SES delivery telemetry, and chat revision flow are documented in
[`docs/MAKER_REVIEW_DELIVERY_REVISION.md`](docs/MAKER_REVIEW_DELIVERY_REVISION.md).

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

### Sign in with Google (optional)

Owners can create an account with a username and password, or with Google. Both
paths land in the same place — an `owner_account` row with no business attached —
and both go through the same invite gate.

**It is off until you configure it.** With no OAuth client the three routes answer
404 and the sign-up page draws no button, so a fresh clone and the current deploy
behave exactly as before.

To turn it on:

1. In the Google Cloud console, **APIs & Services → Credentials → Create
   credentials → OAuth client ID**, type **Web application**.
2. Under **Authorized redirect URIs** paste the stack's `GoogleCallbackEndpoint`
   output verbatim — Google string-matches it, so a missing `/v1` or a trailing
   slash is a `redirect_uri_mismatch`.
3. Put five values in Parameter Store under `/brasstacks/`:
   `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
   `GOOGLE_OAUTH_REDIRECT_URI` (the same URL as step 2),
   `GOOGLE_OAUTH_STATE_SECRET` (any long random string), and
   `BRASSTACKS_SITE_URL` if it is not already there.
4. Re-run the frontend deploy. It checks for `GOOGLE_OAUTH_CLIENT_ID` and draws
   the button only if it exists.

The consent screen needs only the default `openid email profile` scopes. This
flow reads an identity and stores no Google access token, so it does not need
verification review.

### Publish Maker posts to Google Business Profile (optional)

Maker can now turn a `google_business_post` artifact into one owner-confirmed
public action. The owner connects a tenant-owned Business Profile, chooses the
exact location, reviews the exact artifact revision, and clicks **Publish now**.
The provider receipt is stored in `tool_execution`; the recommendation becomes
`live`, and Meter starts its window from the execution timestamp rather than the
draft timestamp. Copy remains available as a fallback.

This is a separate authorization from Sign in with Google. Enable the Google
Business Profile APIs for the same Cloud project, obtain Google approval for API
access, and add the stack's `GoogleBusinessCallbackEndpoint` output to the OAuth
client's Authorized redirect URIs. Store that exact URL as
`/brasstacks/GOOGLE_BUSINESS_REDIRECT_URI`. The existing OAuth client ID, client
secret, state secret and site URL are reused. The CloudFormation stack creates a
dedicated KMS key and supplies its ARN to the execution Lambda. Refresh tokens
are encrypted with a tenant-bound encryption context before CockroachDB storage;
no token is returned to the browser or included in a Maker prompt.

Run `python db/migrate.py --schema-only` in CI before `sam deploy`. The migration
adds `external_connection`, `find.executed_at`, and the immutable link from the
find to its successful `tool_execution` receipt.

### Telling the Meter what a move actually earned

The Meter's honest default reports **no data**, which produces an `estimated`
verdict rather than a win. That is deliberate — but on its own it means no find
can ever be `verified`, and the published hit rate stays undefined forever. A
small business has no payments API to integrate; what it has is an owner who can
count.

So the **Growth** tab carries a result box on every approved move: an amount, the
period it covers (a day, a week, or a month), and optionally how they know. It
posts to `POST /finds/{find_id}/outcome`, which writes one append-only
`find_outcome` row scoped to the caller's session.

Three things it deliberately does not do:

- **It does not score anything.** The row waits; the Meter judges it when the
  measurement window closes. A figure reported on day two of fourteen is not a
  result, and the interface says when the verdict is due rather than implying one.
- **It does not move the forecast or the record.** The Growth headline stays a
  sum of predictions and the chart stays drawn from the ledger. Nothing the owner
  types can increase the verified figure.
- **It cannot rewrite a measured verdict.** An `estimated` row is replaced when a
  real figure arrives afterwards — that is the common case, since a find often
  comes due before anyone has counted. A `verified` or `miss` row is final, in the
  repository as well as in the UI.

Money is entered as text and parsed to integer cents with `Decimal` on the
server. The browser never multiplies by 100: `12.34 * 100` is 1233.9999999999998
in JavaScript, and that is exactly how a cent goes missing.

The migration adds the `find_outcome` table.

Three details that are deliberate rather than incidental:

- **The invite code is still required.** It is a spend control, not a formality —
  a workspace created through Google runs the same nightly Tavily search, ~50
  embeddings and Claude call as any other. The code is checked *before* the
  redirect and carried across the round trip inside the HMAC-signed `state`, so
  it cannot be edited in the browser on the way back.
- **The callback never returns a session token.** It is a redirect, and a token
  in a redirect is a token in browser history and in the next request's
  `Referer`. It hands over a one-time code instead — two minutes, single use,
  stored only as a SHA-256 — which the landing page trades for a real token over
  POST. Same rule the rest of the app follows: a database read must not be enough
  to impersonate an owner.
- **Accounts are keyed on Google's subject, never on the email address.** An
  address here is not unique (several seeded tenants share one inbox), so it
  identifies nobody, and matching on it would let an address that changed hands
  inherit somebody's business. Signing in with Google therefore always yields its
  own account and never adopts an existing password account.

There is still no password reset, no email verification of our own, and no
lockout after repeated failures — see `backend/src/brasstacks/auth.py`. Google
sign-in does not change that; it sidesteps it for the owners who use it.

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

db/schema.sql        19 tables, including decision_event/work_task/task_event/tool_execution
                     and find_outcome, the owner's measured results
db/fixtures/         the exported demo tenant the site build reads

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

The first constrained execution tool sends a concise review notification to a configured inbox through
Amazon SES. The complete working artifact stays in Brass Tacks. A configuration set and EventBridge
destination record sent, delivered, opened, clicked, bounce, complaint, and delay events when SES
publishes them. Owners can request a new version through chat without losing prior artifacts or
receipts. The implementation and acceptance test are in
[`docs/MAKER_REVIEW_DELIVERY_REVISION.md`](docs/MAKER_REVIEW_DELIVERY_REVISION.md); the broader
AgentCore/OAuth/browser roadmap remains in
[`docs/MULTI_TENANT_AGENT_PLATFORM.md`](docs/MULTI_TENANT_AGENT_PLATFORM.md).

## Tests

```bash
python -m pytest backend/tests -q      # 690 offline tests in this version
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

### Deploying

One command, from a machine with Docker, the SAM CLI and AWS credentials:

```bash
python scripts/deploy.py                 # the board, plus the Lambdas if they changed
python scripts/deploy.py site            # the board only
python scripts/deploy.py backend --force # the Lambdas, whether or not they changed
```

It runs the test suite first, fingerprints everything that goes into the Lambda
image, and **skips the image build entirely when nothing in it changed** — which
is the difference between fifteen seconds for a CSS edit and five minutes. Then
it publishes the board (build with live endpoints, sync, invalidate CloudFront)
and verifies what it left behind: stack status, the nightly schedule, and an
HTTP check on the deployed page.

Three sharp edges it exists to blunt, each of which has drawn blood:

- **`sam deploy --parameter-overrides` re-splits its argument on whitespace.**
  A shell's quotes are stripped before SAM sees them, so `cron(0 18 * * ? *)`
  arrives as `cron(0`, EventBridge rejects it, and the stack update rolls back
  taking every Lambda with it.
- **SAM lives behind a space** (`C:\Program Files\...\sam.cmd`), which Git Bash
  cannot launch. Nothing here goes through a shell.
- **`sam deploy` does not touch the site.** The board is static files behind a
  24-hour cache; deploying the Lambdas alone changes nothing a visitor sees.

The GitHub workflows still exist as a fallback and are `workflow_dispatch` only.

### Turning the autopilot on

The nightly schedule is what makes this a loop rather than a button, and it
**ships disabled**: `ScheduleState` defaults to `DISABLED` in
`deploy/template.yaml`, because a deploy that silently started spending on
embeddings and Claude calls every night is a thing you would discover from a
bill. Turning it on is meant to be a deliberate act.

The `Deploy Brass Tacks` workflow asks for it explicitly — **Run workflow** takes
`schedule_state` (defaults to `ENABLED`) and `schedule_expression` (defaults to
`cron(0 18 * * ? *)`, read in the stack's `ScheduleTimezone`). Both are sent to
CloudFormation on every deploy, because `sam deploy` reuses a stack's previous
value for any parameter it is not given: a changed template default never
reaches a stack that already exists.

18:00 rather than dawn, and that is a data decision. The only three sweeps that
ran on the original `cron(0 6 * * ? *)` fired at 06:28, 08:07 and 08:40 local, so
every observation was captured while its tenant was closed — Radar read shut
storefronts, "not available right now" reached the Analyst as an outage, and a
find was published on it. 18:00 is inside trading hours for a restaurant, a shop
and a salon alike, and the owner still wakes up to finished work.

To confirm what is actually deployed:

```bash
aws scheduler get-schedule --name NightFunctionNightly --region us-east-1 \
  --query "{State:State,Expr:ScheduleExpression,Tz:ScheduleExpressionTimezone}"
```

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

**Her ledger is seeded, backdated history, and the figures are illustrative.** A
verified verdict takes real elapsed time — a prediction is stored on one night and
only scored once its measurement window closes days later — which no live demo can
wait out. So `scripts/seed.py` plants a completed history: finds dated weeks back,
their windows already elapsed, with owner-measured outcomes. What is compressed is
the clock, not the mechanism. The embeddings are real Titan vectors, the retrieval
similarities are computed against them, and the Meter genuinely reads each prior
prediction back out of CockroachDB and scores it — the demo shows the memory layer
doing the one thing a stateless agent cannot, on real rows, with only the passage of
time stood in for.

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
