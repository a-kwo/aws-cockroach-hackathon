# Deploying Brass Tacks

The Lambda entry points use the same container build context but have separate
commands and deployment images. `night` runs Radar, Analyst and Meter on a
schedule; authenticated `decision` and `ask` requests create or reuse a durable
CockroachDB task; SQS FIFO buffers approved work; a Task Starter begins one
Step Functions Standard execution; the atomic `maker` worker creates one draft;
`maker_email` optionally sends the review link through SES; and a SQL-only
reconciler recovers missed dispatches and expired leases every five minutes.
`workflow` projects current owner and operator state without invoking a model.

The board still ships a static CockroachDB snapshot for instant first paint,
then Memory Engine revalidates that snapshot through the read-only workflow
route while the operator view is open. See
[`../docs/MULTI_TENANT_AGENT_PLATFORM.md`](../docs/MULTI_TENANT_AGENT_PLATFORM.md)
for the task contract, idempotency model, tool roadmap and acceptance tests.

Prerequisites: AWS SAM CLI, Docker, and an AWS profile that is **not** root.

---

## 1. Stop using root credentials

Do this before anything else. A deploy run under root credentials is a security
problem that no amount of later tidying undoes.

```bash
aws iam create-user --user-name brasstacks-deploy
aws iam attach-user-policy --user-name brasstacks-deploy \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess
aws iam create-access-key --user-name brasstacks-deploy
```

Put the resulting key in `~/.aws/credentials` under `[default]`, **replacing** the
root credentials rather than sitting beside them as a named profile. Nothing then
needs a `--profile` flag it can silently forget, and the root key stops being
present on the machine at all.

Not in `.env`: that file lives inside a repository which is going public, and
"gitignored" is one mistake away from "committed". `~/.aws/` is outside the repo,
which makes it structurally safe rather than conventionally safe. The deploy
tooling reads `~/.aws/` regardless and never looks at `.env`.

**Overwriting the local file does not revoke the root key** — it still exists in
AWS and still works. Once the new key is confirmed working, delete the root
access key in the console. AWS's own guidance is that root should hold none.

`AdministratorAccess` rather than `PowerUserAccess`: SAM creates an execution
role per function, and PowerUser excludes IAM entirely, so the deploy fails at
role creation. A hand-scoped policy is the right long-term answer and a poor use
of a three-week runway.

This is not least privilege and is not pretending to be. What it buys is real
anyway: an IAM access key can be deleted and reissued in seconds, whereas root
cannot be meaningfully restricted, holds billing and account-closure powers no
policy limits, and — with this repository going public — is the credential you
least want to discover in a commit.

## 2. Create the MCP service account

In the **CockroachDB Cloud Console**: create a service account, scope its Cloud
RBAC to this cluster only, and generate the MCP config snippet. Keep the API key
for step 3.

Read-only is the server's default and we never grant write consent — an agent
that can answer questions about the ledger must not be able to edit it.

Assign **Cluster Operator**, scoped to this cluster.

**Not Cluster Developer** — that is below the threshold for the Cloud API's
list/read-cluster operations, and the failure is quiet in an expensive way: the
key authenticates (HTTP 200), `list_clusters` returns an empty array, and the
agent reports that it cannot find any database. Nothing in that chain looks like
a permissions error. Verify with:

```bash
curl -s https://cockroachlabs.cloud/api/v1/clusters \
  -H "Authorization: Bearer $COCKROACH_MCP_TOKEN" | jq '.clusters | length'
```

Zero means the role, not the key. If the query tools still fail on Operator,
`Cluster Admin` scoped to the same single cluster is the next step — at the cost
that read-only then rests on the MCP server's default rather than on the role
itself.

**Auth shape — confirmed, no longer an open question.** The Cloud API
authenticates service-account keys as `Authorization: Bearer {secret_key}`, and
Anthropic's MCP connector renders `authorization_token` into exactly that header.
So the raw secret key goes into `COCKROACH_MCP_TOKEN` verbatim — no `Bearer`
prefix, no encoding. Adding the prefix by hand produces `Bearer Bearer …` and a
401 that reads like a bad key.

## 3. Put the secrets in Parameter Store

Core values live under the SSM prefix rather than in committed files. Keep credentials as SecureString; non-secret feature flags and identifiers may use String.

```bash
put() { aws ssm put-parameter --type SecureString \
          --overwrite --name "/brasstacks/$1" --value "$2"; }

put COCKROACH_DATABASE_URL 'postgresql://…?sslmode=verify-full'
put ANTHROPIC_API_KEY      'sk-ant-…'
put COCKROACH_MCP_TOKEN    '…'          # from step 2
put BRASSTACKS_BUSINESS_ID '…'          # printed by scripts/seed.py
```

For a multi-owner operator portfolio, add a comma-separated allowlist. The live
workflow endpoint never accepts an arbitrary tenant id from the request:

```bash
aws ssm put-parameter --type String --overwrite \
  --name /brasstacks/BRASSTACKS_OPERATOR_BUSINESS_IDS \
  --value 'owner-uuid-1,owner-uuid-2'
```

Leave it absent for the single demo tenant; `BRASSTACKS_BUSINESS_ID` is then the
allowlist automatically.

Two more, not secrets but needed — without them the Ask agent rediscovers the
cluster and schema on every question (measured: 8 tool calls per question
instead of 2):

```bash
aws ssm put-parameter --type String --overwrite \
  --name /brasstacks/COCKROACH_CLUSTER_ID --value '…'
aws ssm put-parameter --type String --overwrite \
  --name /brasstacks/COCKROACH_DATABASE --value defaultdb
```

### Configure the first Maker execution tool: SES review email

Maker always stores the draft first. Email notification is optional and disabled
by default. The model cannot choose the sender or recipient; trusted server
configuration does.

Verify a sender identity in Amazon SES in `us-east-1`. If the SES account is
still in the sandbox, also verify the test recipient `virtual.icfd@gmail.com`.
Then set:

```bash
aws ssm put-parameter --type String --overwrite \
  --name /brasstacks/MAKER_EMAIL_ENABLED --value true

aws ssm put-parameter --type String --overwrite \
  --name /brasstacks/MAKER_EMAIL_FROM \
  --value '<verified-sender@example.com>'

aws ssm put-parameter --type String --overwrite \
  --name /brasstacks/MAKER_REVIEW_EMAIL \
  --value 'virtual.icfd@gmail.com'
```

The email includes the complete draft, task receipt and a link to
`/app/?task=<task-id>`. After sign-in, the app opens the exact recommendation and
full Maker draft. This proves a visible external action and manual-post handoff;
it does not claim that Maker published to a third-party account.

Leave `MAKER_EMAIL_ENABLED=false` while the SES identity is unverified. The
workflow records a `skipped` tool receipt without failing the completed draft.

### Omit `sslrootcert` from the connection string

**As of 2026-08-02 this is handled in code and you should not set it by hand.**
`config.with_ca_bundle()` rewrites `sslrootcert` at startup to a CA bundle that
exists on the machine actually running — certifi's, normally — and `db/migrate.py`
does the same. Leave the parameter off and the same `.env` works on a laptop and
in Lambda.

The history is worth keeping, because the failure is misleading in both
directions. `sslrootcert` used to have to differ by environment:

| | `sslrootcert` |
|---|---|
| Local (Windows) | absolute path to `cockroach-certs/ca.crt`. `system` does not work — psycopg's bundled libpq will not resolve it to the Windows trust store |
| Lambda (Linux) | `/etc/pki/tls/certs/ca-bundle.crt` |

**`sslrootcert=system` fails on Lambda too**, which is counter-intuitive enough
to be worth stating plainly. The container *does* carry OS CA bundles at all
three standard locations, and the cluster presents an ordinary **Let's Encrypt**
certificate. The problem is that `psycopg[binary]` bundles its own OpenSSL,
whose compiled-in default cert path does not exist in the Amazon Linux image, so
`system` resolves to nothing. Naming a real bundle is the fix.

Two things forced the code fix. `cockroach-certs/ca.crt` is gitignored, so it is
absent from a fresh clone; and a `.env` shared between developers carries
whichever absolute path its author happened to have. Both surface as a TLS
error, which sends the reader to `sslmode` and the certificate chain rather than
to the missing file. That file was only ISRG Root X1 — the public Let's Encrypt
root — so nothing about the cluster required a private copy of it.

`sslmode=verify-full` is retained throughout, and the rewrite never touches it —
the aim was never to weaken TLS. An `sslrootcert` that does point at a real file
is left exactly as configured.

Do **not** `COPY cockroach-certs/ca.crt` into the image instead. That file is
gitignored, so the build would fail for everyone but this machine, a judge
included.

### Rotating a secret needs a cold start

`secrets.py` loads Parameter Store into the environment once per execution
environment and then leaves it alone, so a warm container keeps serving the old
value after you rotate one. Symptom: you fix a parameter, redeploy nothing, and
the same error persists at suspiciously low latency (~200ms — it never reached
the network). Force new containers:

```bash
aws lambda update-function-configuration --function-name <fn> \
  --environment "Variables={DEPLOY_NONCE=$(date +%s),...}"
```

Any configuration change replaces every execution environment. This is a
deliberate trade — reading SSM on every invocation would add latency and cost to
each request to save a step that happens rarely.

## 4. Apply the task schema and create the artifact bucket

Apply the additive schema before shifting traffic. Lambda also contains an
idempotent bootstrap as a safety net, but production deployment should run the
migration explicitly:

```bash
python db/migrate.py --schema-only
aws s3 mb s3://brasstacks-artifacts-<suffix>
```

The migration adds `work_task`, `task_event`, `tool_execution`, and task/body
linkage on `artifact`.

## 5. Deploy

From the repository root, not this directory — the Docker build context is the
repo root so the image can carry both `backend/src` and the seed corpus.

```bash
sam build --template deploy/template.yaml

sam deploy \
  --template .aws-sam/build/template.yaml \
  --stack-name brasstacks --region us-east-1 \
  --capabilities CAPABILITY_IAM \
  --resolve-image-repos --resolve-s3 \
  --no-confirm-changeset --no-fail-on-empty-changeset \
  --parameter-overrides ArtifactBucket=<your-bucket>
```

`--resolve-image-repos` creates the ECR repositories the container functions
need; `--resolve-s3` handles the deployment bucket. `sam deploy --guided` is the
interactive equivalent and asks the same questions.

It prints the site/API outputs plus the Maker function, Step Functions workflow, FIFO queue and DLQ identifiers on completion.

## 6. Prove it works

```bash
# A night, on demand, rather than waiting until 6 AM.
aws lambda invoke \
  --function-name <NightFunctionName from the stack outputs> /dev/stdout

# One owner question, end to end.
curl -sS -X POST <AskEndpoint> \
  -H 'Content-Type: application/json' \
  -d '{"question":"how much have I actually made?"}' | jq

# Current operator state, directly from CockroachDB.
curl -i -sS <WorkflowEndpoint>
```

The Ask response carries `trail` — the SQL the agent ran to answer. If `trail` is
empty and `queried_the_cluster` is `false`, the model answered from its own
knowledge instead of from the database. That is a prompt failure, not a success,
and it is the thing to watch for on the first live call.

Then run one task end to end:

1. Sign in and press **Do it** once.
2. Confirm the card immediately shows Saving, then Approved.
3. Confirm Memory Engine shows one exact task with a workflow receipt.
4. Confirm one—not two—draft artifacts are created.
5. If SES is enabled, confirm one message arrives at `virtual.icfd@gmail.com`.
6. Open the email link, sign in, and verify the exact task and full draft open.
7. Wait past one reconciliation interval and verify no duplicate draft or email appears.

Then confirm the schedule fires unattended overnight. That is the "autonomous
rather than a button" claim, and it is the one part that cannot be faked on
video.

---

## Notes

- **CockroachDB is the task source of truth.** SQS and Step Functions provide
  delivery and orchestration. Every worker must atomically claim the task before
  constructing a model client or executing a tool.
- **Delivery may be repeated.** SQS/Lambda and workflow retries are treated as
  at-least-once. Unique task, artifact and tool-execution keys provide
  idempotent business behavior; the system does not claim end-to-end magical
  exactly-once side effects.
- **The email is a review notification, not public publishing.** OAuth account
  connections and browser automation remain roadmap work and must keep
  credentials outside model prompts.
- **Container images, not zips.** `psycopg[binary]` ships platform-specific
  wheels and this repo is developed on Windows; a zip built there installs
  Windows wheels and fails on Lambda's manylinux runtime.
- **The source tree keeps its repository shape inside the image** rather than
  being pip-installed. `brasstacks.night` derives paths from its own location,
  and installing into site-packages would resolve them somewhere else.
  `BRASSTACKS_CORPUS_PATH` is set explicitly so this does not depend on the
  directory depth staying constant.
- **Radar uses the committed corpus only.** Live web search stays opt-in: the
  demo tenant is fictional, so searching for it returns trade-show videos and
  years-old market reports that pollute the memory layer and cost embedding
  spend. Measured on the first live run: 40 of 40 web signals irrelevant.
- **The shared HTTP API is throttled to 2 rps / burst 5.** Ask also bounds the
  question at 500 characters because it proxies a paid model from a public URL.

## Connect For You and live Memory Engine

`Do it` and `Pass` use the `DecisionFunction` HTTP route:

```text
POST /v1/finds/{find_id}/decision
{"decision":"approved"}
```

After `sam deploy`, copy the `DecisionEndpoint` and `WorkflowEndpoint` stack
outputs and rebuild the site with them:

```bash
DECISION_API_ENDPOINT="https://YOUR_API_ID.execute-api.YOUR_REGION.amazonaws.com/v1" \
WORKFLOW_API_ENDPOINT="https://YOUR_API_ID.execute-api.YOUR_REGION.amazonaws.com/v1/workflow" \
  python scripts/build_web.py
```

Because both routes share the same API, `scripts/build_web.py` also infers
`$DECISION_API_ENDPOINT/workflow` when `WORKFLOW_API_ENDPOINT` is omitted.

The built `web/app/index.html` then writes decisions to CockroachDB. `Do it`
changes the find to `accepted`, creates or reuses one idempotent `work_task`,
and sends one dispatch to SQS FIFO. `Pass` changes the find to `rejected` and
creates no Maker task. From the passed recommendation's chat drawer, `Undo Pass`
records a new `accepted` decision, preserves the earlier Pass receipt in
conversation memory, and uses the same task key.

The FIFO message starts a Step Functions Standard execution. Maker must atomically
claim the CockroachDB task before it constructs the reasoner, so duplicate queue
or workflow delivery consumes no second model call and creates no second draft.
A five-minute SQL-first reconciliation sweep creates tasks for legacy approved
finds, recovers expired claims and re-dispatches retryable rows. An empty sweep
invokes no reasoning model and consumes zero LLM tokens. Without the endpoint
environment variable, the UI remains usable in an explicitly labelled demo-only
mode and does not pretend the decision was persisted.

The app performs one `GET /v1/workflow` sync at startup so For You reflects
decisions made on another device. Memory Engine then revalidates at the
server-provided cadence while its tab is visible (15 seconds by default). The
response includes current find status, agent runs, token receipts, evidence,
Maker artifacts, and Meter verdicts for the configured owner allowlist. It does
not return embeddings or the full observation corpus, and it never invokes a
model: each refresh consumes zero LLM tokens.

The browser sends `If-None-Match` on later reads. An unchanged CockroachDB
snapshot returns `304 Not Modified`, and polling stops whenever the operator tab
is hidden or the user leaves Memory Engine. If the endpoint is temporarily
unavailable, the last good live state remains visible and is marked stale;
before the first successful read, the build snapshot remains visible instead.

## Production authentication boundary

The current application handlers require Brass Tacks bearer sessions and derive
the business from the authenticated account; they do not trust a browser-supplied
`business_id`. API Gateway itself does not yet enforce a managed JWT authorizer.
Before using real owner data at production scale, put a managed identity boundary
(for example Cognito or another OIDC provider) in front of owner routes, map claims
to allowed businesses server-side, and retain the same repository-level tenant
checks. Do not put passwords, provider tokens or permanent API secrets in static
HTML or model prompts.
