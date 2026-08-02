# Deploying Brass Tacks

The Lambda entry points share one container image. `night` runs Radar, Analyst
and Meter on a schedule; `maker` starts as soon as the owner chooses Do it and
also reconciles accepted backlog every five minutes; `ask` answers owner
questions and can execute an authenticated Undo Pass; `decision` persists Do it
/ Pass; and `workflow` serves current operator state. The board still ships a
static CockroachDB snapshot for instant first paint, then Memory Engine
revalidates that snapshot through the read-only workflow route while the
operator view is open.

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

Four values, all SecureString. They live here rather than in Lambda environment
variables, where they would be readable from the console.

```bash
put() { aws ssm put-parameter --type SecureString \
          --overwrite --name "/brasstacks/$1" --value "$2"; }

put COCKROACH_DATABASE_URL 'postgresql://…?sslmode=verify-full&sslrootcert=system'
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

## 4. Create the artifact bucket

```bash
aws s3 mb s3://brasstacks-artifacts-<suffix>
```

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

It prints the Ask, Decision, and Workflow endpoint URLs on completion.

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

Then confirm the schedule fires unattended overnight. That is the "autonomous
rather than a button" claim, and it is the one part that cannot be faked on
video.

---

## Notes

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
changes the find to `accepted` and asynchronously starts the dedicated Maker
worker. `Pass` changes it to `rejected`. From the passed recommendation's chat
drawer, `Undo Pass` records a new `accepted` decision, preserves the earlier
Pass receipt in conversation memory, and starts Maker. Accepted rows with no
artifact are the durable queue; a five-minute SQL-first reconciliation sweep
recovers a missed invocation and drains older approved work. An empty sweep
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

The submitted demo uses fictional business data and leaves the shared HTTP API
public, with throttling and a server-side tenant allowlist. The allowlist prevents
arbitrary `business_id` enumeration, but it is **not user authentication**. Before
using real owner data, put API Gateway JWT authorization (for example Amazon
Cognito) in front of `Ask`, `Decision`, and `Workflow`, and derive the permitted
business ids from authenticated claims rather than from a browser-supplied value.
Do not put a permanent API secret in the static HTML; anyone can inspect it.
