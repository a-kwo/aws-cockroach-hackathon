# Deploying Brass Tacks

Two Lambdas: `night` runs the loop on a schedule, `ask` answers owner questions
over CockroachDB's managed MCP server. Everything else — the board — is a static
build and needs no runtime.

Prerequisites: AWS SAM CLI, Docker, and an AWS profile that is **not** root.

---

## 1. Stop using root credentials

Do this before anything else. A deploy run under root credentials is a security
problem that no amount of later tidying undoes.

```bash
aws iam create-user --user-name brasstacks-deploy
aws iam attach-user-policy --user-name brasstacks-deploy \
  --policy-arn arn:aws:iam::aws:policy/PowerUserAccess   # narrow this before submission
aws iam create-access-key --user-name brasstacks-deploy
aws configure --profile brasstacks
```

## 2. Put the secrets in Parameter Store

Four values, all SecureString. They live here rather than in Lambda environment
variables, where they would be readable from the console.

```bash
put() { aws ssm put-parameter --profile brasstacks --type SecureString \
          --overwrite --name "/brasstacks/$1" --value "$2"; }

put COCKROACH_DATABASE_URL 'postgresql://…?sslmode=verify-full'
put ANTHROPIC_API_KEY      'sk-ant-…'
put COCKROACH_MCP_TOKEN    '…'          # see step 3
put BRASSTACKS_BUSINESS_ID '…'          # printed by scripts/seed.py
```

## 3. Create the MCP service account

In the **CockroachDB Cloud Console**: create a service account, scope its Cloud
RBAC to this cluster only, and generate the MCP config snippet. Copy the API key
into `COCKROACH_MCP_TOKEN` above.

Read-only is the server's default and we never grant write consent — an agent
that can answer questions about the ledger must not be able to edit it.

> **The one thing to check by hand.** The Console's generated snippet is the
> authoritative source for the auth header shape. `McpAsker` sends the key as
> `authorization_token` on the MCP server declaration, which is what Anthropic's
> connector expects; if the Console shows something different, that is the place
> the two have to be reconciled.

## 4. Create the artifact bucket

```bash
aws s3 mb s3://brasstacks-artifacts-<suffix> --profile brasstacks
```

## 5. Deploy

From the repository root, not this directory — the Docker build context is the
repo root so the image can carry both `backend/src` and the seed corpus.

```bash
sam build --template deploy/template.yaml
sam deploy --guided --profile brasstacks
```

`--guided` will ask for `ArtifactBucket`; the rest have defaults. It prints the
Ask endpoint URL on completion.

## 6. Prove it works

```bash
# A night, on demand, rather than waiting until 6 AM.
aws lambda invoke --profile brasstacks \
  --function-name <NightFunctionName from the stack outputs> /dev/stdout

# One owner question, end to end.
curl -sS -X POST <AskEndpoint> \
  -H 'Content-Type: application/json' \
  -d '{"question":"how much have I actually made?"}' | jq
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
- **The Ask endpoint is throttled to 2 rps / burst 5** and bounds the question
  at 500 characters. It proxies a paid model from a public URL.
