# Brass Tacks

> Found money, on autopilot.

A team of AI agents that grows a small business while the owner runs it. The agents
find revenue opportunities, do the work, and prove whether it paid — or publish the miss.

Built for the **AWS + CockroachDB hackathon**. Target: submission in 2–4 weeks from
2026-07-27.

---

## Objective

Ship an agentic application that uses **CockroachDB as its persistent memory layer**
and is **deployed on AWS**, where the memory layer is not decoration — the product is
impossible without it.

The core claim: an agent that remembers what it predicted last night, and checks today
whether it was right, is categorically different from a chatbot. Every architectural
decision should serve that claim.

### The spine

Everything else is optional polish. This loop must work end to end:

```
EventBridge Scheduler  (nightly "6 AM Radar")
  └─> RADAR    observe rivals / reviews / trends
               → Bedrock embeddings → INSERT into CockroachDB (vector + relational)
  └─> ANALYST  vector search over ALL accumulated memory
               → Bedrock (Claude) generates a "find"
               → write a PREDICTION row: "$23/day, verify after <date>"
  └─> METER    read PRIOR runs' predictions out of CockroachDB
               → compare against observed outcome
               → VERIFIED / ESTIMATED / MISS → the ledger
```

The Meter is only possible because the prediction was durably stored on a previous run.
That is the memory layer doing something stateless agents cannot. It is also the money
shot for the demo video.

---

## Non-negotiable constraints (from the hackathon rules)

- **CockroachDB is the memory layer.** Do not introduce a second store for vectors,
  sessions, or state. Specifically: **do not use Bedrock Knowledge Bases** — it would
  put a competing vector store next to Cockroach and undercut the entire entry.
- **AWS powers retrieval and the runtime. CockroachDB is the brain.** Bedrock's Titan
  model generates every embedding the vector index holds, so AWS is load-bearing for
  memory retrieval specifically. Keep that boundary clean; both required disclosure
  sections depend on it.
- **Reasoning runs on the Anthropic API, not Bedrock** — forced, not preferred. AWS
  could not grant this account access to any current Claude model (see Model access
  below). All model calls go through a provider interface so this is a config change
  if Bedrock access ever lands.
- **≥2 CockroachDB tools, meaningfully used at runtime.** The judging question is
  literally *"What did the agent actually do with the tool?"* — dev-time-only usage is
  a weak answer.
- **≥1 AWS service**, meaningfully integrated.
- The repo must be public, open source (MIT), with setup + run instructions that a judge
  can actually follow.
- The demo video must **show the CockroachDB memory layer actively working**.

### Tool disclosure targets

| CockroachDB tool | Runtime role |
|---|---|
| Distributed Vector Indexing | Radar embeds observations; Analyst semantic-searches accumulated memory before every find |
| Cloud Managed MCP Server | The **Ask** agent answers owner questions by querying the live cluster read-only over MCP |
| ccloud CLI | Setup automation — provision cluster, configure networking, schedule backups |

| AWS service | Role | Why it is load-bearing |
|---|---|---|
| Bedrock | Titan Text Embeddings V2 | Generates every vector in the index. No embeddings → no retrieval → no memory. |
| Lambda | One function per agent | The whole agent runtime |
| EventBridge Scheduler | The nightly 6 AM wake | What makes the loop autonomous rather than a button |
| S3 | Maker artifacts | The done-for-you deliverables |
| API Gateway | Frontend → backend | How the demo URL reaches the agents |

### Model access — the constraint that shaped this

As of 2026-07-28, AWS account `881550374737` cannot invoke **any** current Anthropic
model on Bedrock. Verified directly, not assumed:

- `agreementAvailability: NOT_AVAILABLE` for every current Claude model in us-east-1,
  us-east-2, and us-west-2, while `regionAvailability`, `entitlementAvailability`, and
  `authorizationStatus` are all green. Only the agreement is missing.
- The error directs to AWS Sales rather than offering a use-case form, which means the
  account cannot self-accept the agreement.
- A support grant did arrive, but for `claude-3-haiku-20240307` and
  `claude-3-sonnet-20240229` — both **retired**, both returning
  `ResourceNotFoundException` on invoke. Agreement records outlived the models.
- 27 non-Anthropic Bedrock models invoke fine, so neither Bedrock, the credentials,
  nor the region is at fault.

Consequence: Claude runs via the Anthropic API; Bedrock keeps embeddings. Disclose this
plainly in the README — nothing in the rules requires the LLM to run on AWS, and a
judge finding it unstated is worse than reading it up front.

---

## Stack

- **Backend:** Python. `psycopg` for CockroachDB, `boto3` for Bedrock.
- **Frontend:** React (rebuild — see below).
- **DB:** CockroachDB Cloud, vector indexes with `vector_cosine_ops`.

### Scope cuts (deliberate)

Build Mapper, Radar, Analyst, Meter. **Dropped:** Stripe, accounts, multi-tenancy.
Single demo tenant. Maker ships exactly one artifact type.

---

## Working agreement

### Test-Driven Development — required

Write the test first. Watch it fail. Write the minimum code to pass it. Refactor.

- **No production code without a failing test that demands it.** This applies to agent
  logic, SQL layer, and API handlers alike.
- **Bedrock and CockroachDB calls get faked at the boundary.** Agent reasoning must be
  testable without network access or AWS credentials. A contributor with no cloud
  account should be able to clone the repo and run the full unit suite green.
- **Integration tests are separate and marked**, run against a real cluster
  (local `cockroach demo` or a Cloud cluster). Never mixed into the unit suite.
- **The money math is the highest-risk code.** Predictions, actuals, and ledger
  verdicts get exhaustive tests including the unhappy paths: a prediction with no
  matching outcome, a miss, a partial period, a duplicate observation.
- Money is stored and computed as **integer cents**, never floats. Test this.

### Other standing rules

- **Never commit secrets.** Connection strings and AWS keys go in `.env`, which is
  gitignored. `.env.example` documents the shape with dummy values.
- **Dedup matters.** Radar runs nightly and will re-observe the same reviews. Content
  hashing is part of the design, not an optimization.
- **Every find records its evidence.** The `find_evidence` table stores which
  observations the vector search returned and their similarity scores. This is the
  audit trail that proves retrieval drove the reasoning — and it is what we put on
  screen in the demo.

---

## Provenance / disclosure

`Product Demo/` contains a pitch deck and a clickable front-end mock built before this
project began, for a different competition (since abandoned). They are **design
reference only** — no backend was ever built, and no code from them ships in the
product. This must be disclosed in the README.

All Gemini / XPRIZE branding from those files must stay out of the submission.
