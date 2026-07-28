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
- **AWS does inference. CockroachDB is the brain.** Keep this boundary clean; both
  required disclosure sections depend on it.
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

| AWS service | Role |
|---|---|
| Bedrock | Claude for reasoning, Titan for embeddings |
| Lambda | One function per agent |
| EventBridge Scheduler | The nightly loop |
| S3 | Maker artifacts (menus, draft replies) |
| API Gateway | Frontend API |

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
