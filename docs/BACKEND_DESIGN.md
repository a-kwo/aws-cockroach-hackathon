# Backend design — what remains

> **Historical planning document.** The Ask agent, AWS deployment, Maker/S3,
> authentication, durable multi-tenant task plane, Step Functions workflow, SQS
> FIFO delivery, and first SES tool now exist. For the current architecture and
> remaining roadmap, use
> [`MULTI_TENANT_AGENT_PLATFORM.md`](MULTI_TENANT_AGENT_PLATFORM.md).

Written 2026-07-28. Companion to `CLAUDE.md` (constraints) and `SESSION_SUMMARY.md`
(state). This document covers only the backend work that is still open, and it
exists because two of the open items are **hackathon rules compliance, not polish**.

---

## 0. Where the backend actually stands

The nightly loop is built and tested: Radar → Analyst → Meter, 194 offline unit
tests plus 46 integration tests that run against a live cluster. `night.py` runs the
whole spine end to end on a laptop. That part is done. (Measured 2026-07-28 —
`SESSION_SUMMARY.md` still says 165, which was true when it was written.)

What is not done divides cleanly:

| Gap | Kind | Why it matters |
|---|---|---|
| Ask agent over the Cockroach MCP Server | **Rules compliance** | Only **one** CockroachDB tool is used at runtime today. The rules require ≥2. |
| Nothing deployed to AWS | **Objective** | "Deployed on AWS" is the stated objective; today AWS is one API call from a laptop. |
| Maker / S3 | **Disclosure honesty** | S3 is disclosed as a load-bearing AWS service. Nothing writes to it. |
| Mapper | Scope | Listed in `CLAUDE.md` scope. Not built. Arguably droppable — see §5. |

Everything else — more seed finds, README, video — is execution, not design.

---

## 1. The compliance gap, stated precisely

The rules require **≥2 CockroachDB tools, meaningfully used at runtime**, and the
judging question is literally *"What did the agent actually do with the tool?"*
Against the disclosure table in `CLAUDE.md`:

| Disclosed tool | Runtime today | Verdict |
|---|---|---|
| Distributed Vector Indexing | Radar embeds; Analyst searches every night | ✅ Genuine |
| Cloud Managed MCP Server | Designed, not built | ❌ **Missing** |
| ccloud CLI | Setup only, ad hoc | ⚠️ Dev-time only — the rules call this a weak answer |

So the entry currently discloses three tools and can defend one. **The Ask agent is
the single highest-value piece of backend work remaining**, and it is not optional.

The ccloud row cannot be fixed by building more — a provisioning CLI is setup by
nature. The honest move is to commit the setup automation as a real, runnable
script (§4.4) and let it stand as what it is, with vector indexing and MCP carrying
the "meaningfully used at runtime" claim.

---

## 2. Design: the Ask agent

### 2.1 What it is

The owner types a question — *"how much did the waitlist actually make me?"*, *"what
did you see about Lucca's?"* — and an agent answers it by querying the live cluster
read-only. It is the one place in the product where the owner reaches into the
memory layer directly rather than reading what the nightly run decided to show her.

### 2.2 The transport, verified rather than assumed

CockroachDB Cloud's managed MCP server is a **remote HTTPS endpoint** at
`https://cockroachlabs.cloud/mcp`. Three properties decide the design:

- **Streamable HTTP transport.** SSE is explicitly excluded as deprecated in MCP;
  there is no stdio mode. A remote URL server is exactly — and only — what
  Anthropic's MCP connector accepts.
- **Read-only by default**, with writes opt-in via consent. `DROP`/`TRUNCATE` are
  unsupported outright. The tools that matter to us are `list_databases`,
  `get_table_schema`, and `select_query`.
- **Service account API keys** for autonomous environments, alongside OAuth 2.1 +
  PKCE for interactive ones. A Lambda has no human to complete a PKCE flow, so the
  service account key is the only viable path.

This lines up with the note in `SESSION_SUMMARY.md`, and it is worth stating plainly
in the README because it inverts the project's biggest constraint:

> **The MCP connector is a first-party Anthropic API feature and is not available on
> Bedrock.** Being forced off Bedrock for reasoning is what makes the Ask agent
> possible at all. The constraint that cost us the Bedrock disclosure is the same one
> that buys us the second CockroachDB tool.

### 2.3 The call shape

The MCP connector needs **both halves** or the request is rejected as a validation
error — declaring the server without the toolset is a 400:

```python
client.beta.messages.create(
    model="claude-opus-5",
    max_tokens=2048,
    betas=["mcp-client-2025-11-20"],
    mcp_servers=[{
        "type": "url",
        "name": "cockroach",
        "url": "https://cockroachlabs.cloud/mcp",
        "authorization_token": settings.cockroach_mcp_token,
    }],
    tools=[{"type": "mcp_toolset", "mcp_server_name": "cockroach"}],
    system=ASK_SYSTEM_PROMPT,
    messages=[{"role": "user", "content": question}],
)
```

**The tool calls execute server-side, between Anthropic and CockroachDB Cloud.** Our
Lambda never opens a psycopg connection on this path and never runs the SQL itself.
That is the whole point — it is what makes "the agent queried the live cluster over
the Cloud Managed MCP Server" a true sentence rather than a generous reading of one.

Consequence worth designing around: there is no agentic loop for us to write. The
response comes back with `mcp_tool_use` / `mcp_tool_result` blocks already resolved.
We read the trail; we do not drive it.

### 2.4 Why this needs a new provider interface

`Reasoner.complete_json` does not fit. It returns a validated JSON object and takes
no tools. The Ask agent returns **prose plus a tool trail**, and its failure modes
differ (a tool can fail while the turn succeeds). Bending `complete_json` to carry
it would make the Analyst's contract worse to serve a different agent.

Add a third narrow protocol alongside `Embedder` and `Reasoner` in `providers.py`:

```python
@dataclass(frozen=True)
class ToolCall:
    name: str          # "select_query", "get_table_schema", ...
    input: dict        # what the model asked for — the SQL lives here
    is_error: bool

@dataclass(frozen=True)
class Answer:
    text: str
    tool_calls: tuple[ToolCall, ...]

@runtime_checkable
class Asker(Protocol):
    def ask(self, *, system: str, question: str,
            max_tokens: int = 2048) -> Answer: ...
```

Two implementations, mirroring the existing pattern exactly:

- `McpAsker` — real, wraps the beta call above.
- `FakeAsker` — scripted, offline, same exhaustion-raises-loudly behaviour as
  `FakeReasoner`. This is what keeps the unit suite runnable with no cloud account,
  which is a standing rule and a contributor-facing promise in the README.

`ModelRefusedError` already exists and applies unchanged.

### 2.5 The honesty constraints on this agent

Three, and each is a claim about the owner's money or her data:

1. **Read-only is enforced twice.** The MCP server defaults to read-only, and we
   never grant write consent. Belt and braces: the service account gets a Cloud RBAC
   role scoped to the single demo cluster. An agent that can answer questions must
   not be able to edit the ledger it is answering about.
2. **The tool trail is the receipt.** Same principle as `find_evidence`: an answer
   that cannot show the query that produced it is a chatbot answer. Every Ask turn
   writes an `agent_run` row (the `ask` value already exists in the `agent_kind`
   enum) and records the executed SQL. **The demo shows this on screen.**
3. **It says "I don't know."** The system prompt forbids answering from the model's
   own knowledge of restaurants. If `select_query` returns nothing, the answer is
   that nothing was found — not a plausible number. This is the `PRODUCT.md` voice
   commitment, and it is the failure mode most likely to embarrass us on video.

### 2.6 Storing the trail — recommendation

`agent_run` has `note` but nowhere structured for a tool trail. Two options:

| Option | Cost | Gets us |
|---|---|---|
| **A. Reuse `agent_run.note`** | Zero migration | A human-readable trail in the run row. Not queryable. |
| B. New `ask_turn` table | One migration + repo methods + contract tests | Queryable history, replayable transcript |

**Recommendation: A for the submission.** The demo needs the SQL *visible*, not
*queryable*, and the deadline is 2026-08-18 with a feature freeze on 08-14. B is the
right shape if the product continues; it is not worth a schema migration and a round
of contract tests three weeks out. Write the trail into `note` as one line per tool
call, prefixed so the UI can split it deterministically.

I am flagging this as the one place I'd expect a reviewer to disagree: if you want
the Ask history to survive as a product surface rather than a demo beat, do B now,
because retrofitting it after the video means re-recording the video.

### 2.7 Test plan (TDD, per the working agreement)

Write these first, watch them fail:

- `McpAsker` sends both `mcp_servers` and the matching `mcp_toolset` — the
  validation error this prevents is silent until runtime.
- The beta flag `mcp-client-2025-11-20` is present, and the call goes through
  `client.beta.messages`, not `client.messages`.
- A `refusal` stop reason raises `ModelRefusedError` before `content` is read.
- Tool trail extraction pulls `mcp_tool_use` blocks in order, including a turn where
  one tool errored and the turn still succeeded.
- An answer with **zero** tool calls is surfaced as such — that is the "the model
  answered from its own knowledge" failure, and it must be detectable, not silent.
- The Ask run writes an `agent_run` row with `agent='ask'` and finishes it even when
  the model refuses.

The token is a secret: it never appears in a test fixture, and `Settings.__repr__`
must mask it the way `anthropic_api_key` already is.

---

## 3. Design: AWS deployment

Today AWS is load-bearing in exactly one place — Bedrock Titan generates every
vector — and that call is made from a laptop. Four of the five disclosed AWS
services are aspirational.

### 3.1 Packaging

**Container-image Lambdas, not zip.** `psycopg[binary]` needs manylinux wheels and
the repo is developed on Windows; a zip built on this machine will not run on
Lambda. One base image, one function per agent, differing only in handler. This was
already the conclusion in `SESSION_SUMMARY.md` and I see no reason to revisit it.

### 3.2 Topology

```
EventBridge Scheduler  (cron, nightly)
      └─> radar-fn ──> analyst-fn ──> meter-fn        Step-function-free chain:
                                                       each Lambda invokes the next.
API Gateway (HTTP API)
      └─> ask-fn ──> Anthropic API ──MCP──> cockroachlabs.cloud/mcp
```

**Deliberate omission: no Step Functions.** The chain is three sequential calls with
no branching, no retry policy worth expressing declaratively, and no parallelism.
Step Functions would be a fifth AWS service on the disclosure list that the product
does not need, and "we added it for the disclosure table" is exactly the kind of
answer the judging question is designed to catch. `night.py` already sequences these
three correctly; the Lambda chain is the same order.

### 3.3 The frontend boundary — implemented hybrid model

The original recommendation below was superseded once Do it / Pass became a real
CockroachDB write and the operator view had to reflect work completed later or on
another device. The shipped boundary is now deliberately hybrid:

- `scripts/build_web.py` still splices a CockroachDB export into static HTML. That
  is the immediate, failure-tolerant first paint, and the honesty tests still run
  against it without a network or cloud account.
- `POST /finds/{id}/decision` persists the owner decision before the card moves.
- `GET /workflow` returns a compact, read-only workflow snapshot for a configured
  business allowlist. Memory Engine fetches it only while the operator view is
  visible, uses `ETag` revalidation, and keeps the last good state when the route
  is unavailable.
- The live response carries current finds, evidence receipts, agent runs, token
  usage, Maker artifacts, and Meter verdicts. It intentionally does not return
  embeddings or the full observation corpus.

This keeps the static build's reproducibility without making it the source of
truth for current operations. The frontend contract remains the same on both
paths, so live state is an overlay rather than a second product model.

### 3.4 Secrets

`.env` does not exist on Lambda, and `Settings.load()` already handles that
correctly — real environment variables win and the `.env` fallback is skipped when
the file is absent. So the code needs no change; the deployment does.

**Recommendation: SSM Parameter Store SecureString**, injected at deploy time.
Cheaper than Secrets Manager, no rotation requirement we would actually use, and
adequate for four values: the Cockroach URL, the Anthropic key, the MCP service
account token, and the business id. Never bake them into the image.

Related and unresolved: `SESSION_SUMMARY.md` notes the project is **still on root
AWS credentials**. That must be an IAM user with a scoped policy before any deploy
code is written, not after. It is the one item here that is a security issue rather
than a design decision.

### 3.5 New settings

Additive to `Settings`, following the existing fail-loud-and-name-the-variable
pattern:

| Variable | Required when | Notes |
|---|---|---|
| `COCKROACH_MCP_URL` | Ask enabled | Defaults to `https://cockroachlabs.cloud/mcp` |
| `COCKROACH_MCP_TOKEN` | Ask enabled | Service account key. **Masked in `__repr__`.** |

Both optional at the `Settings` level so the nightly loop still boots without them —
Radar/Analyst/Meter have no business failing because the Ask agent is unconfigured.
`build_asker()` raises the named error if they are missing when actually needed.

---

## 4. The S3 problem — flagging, not solving

`CLAUDE.md` discloses S3 with the role "Maker artifacts — the done-for-you
deliverables," and the `artifact` table exists with `s3_bucket` / `s3_key` columns.
**No Maker agent exists and nothing writes to S3.**

`PRODUCT.md` does not claim the Maker under confirmed capabilities, so the product
doc is honest. The disclosure table is not. Three ways out:

| Option | Effort | Honest? |
|---|---|---|
| **A. Build a minimal Maker** — one artifact type, per the existing scope cut | ~half a day | ✅ Restores the claim |
| B. Drop S3 from the disclosure | minutes | ✅ Four AWS services is still comfortably ≥1 |
| C. Ship as-is | zero | ❌ A judge who greps for S3 finds nothing |

**Recommendation: A if the schedule holds, B the moment it slips.** C is not
available. The scope cut in `CLAUDE.md` already says "Maker ships exactly one
artifact type" — a draft review reply written to S3, previewed in the UI, is the
cheapest version of that and it reuses the `artifact` table as designed.

**Mapper: recommend dropping it explicitly** and amending the scope line in
`CLAUDE.md`. Its job — chat → business profile — is already served by the seeded
`business_fact` rows, and it contributes nothing to either required disclosure. A
scope line that names an agent nobody built is a worse look than a scope line that
says it was cut.

---

## 5. Build order

Sequenced by risk, not by convenience. Items 1–2 are the ones that change whether
the entry is compliant.

1. **Ask agent, TDD, offline.** `Asker` protocol, `FakeAsker`, `McpAsker`, unit
   tests. No cloud account needed to finish this step.
2. **Ask against the live MCP endpoint.** Provision the service account, scope its
   RBAC to the demo cluster, confirm the auth header shape from the Cloud Console
   (the blog post does not document it — this is the one genuine unknown left, and
   it is a five-minute lookup, not a design risk).
3. **IAM user.** Retire root credentials before writing deploy code.
4. **Deploy.** Container images, EventBridge Scheduler, and API Gateway routes
   for Ask, Decision, and the read-only Workflow snapshot.
5. **Maker + S3**, or amend the disclosure. Decide by 08-07 so the video is filmed
   against whatever is true.
6. **README + disclosures.** Both required sections depend on 1–5 being settled.

Feature freeze 08-14, record, submit 08-17. The deadline risk on this list is item
4, not item 1 — deployment is the part with unknown-unknowns, and it is fifth.

---

## Appendix: unresolved questions

- **MCP auth header name.** The Cloud Console generates the config snippet; the blog
  post does not print the header. Resolve by generating a snippet before writing
  `McpAsker`, so the first live call is not a guess.
- **MCP tenant scoping.** `select_query` runs with the service account's RBAC, not
  our application's `business_id` filter. Harmless for a single-tenant demo,
  load-bearing the moment there is a second tenant. Worth one sentence in the README
  rather than silence.
- **Ask rate limiting.** A public demo URL that proxies to a paid model with no
  limit is a bill waiting to happen. API Gateway throttling is the cheap answer;
  needs a number chosen before the URL is public.
