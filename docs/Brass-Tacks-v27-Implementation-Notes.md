# v27 implementation notes — durable multi-tenant agent tasks

This change attaches the first production-shaped task workflow to Maker while preserving the existing Radar, Analyst, Ask and Meter behavior.

## Included

- CockroachDB `work_task`, `task_event` and `tool_execution` tables.
- Idempotent task and artifact keys.
- SQS FIFO queue and dead-letter queue.
- Step Functions Standard Maker workflow.
- Atomic Maker claim with lease/retry recovery.
- SQL-only five-minute reconciliation.
- Exact task visibility in Memory Engine.
- Immediate Saving / Approved / Passed feedback.
- Optional SES review email to the configured owner/test inbox.
- Deep link to the exact task and complete draft.
- Unit and contract tests for duplicate delivery, retry, tenant scoping and tool receipts.

## Not included yet

- OAuth connections to Google, Meta, Yelp, POS or scheduling providers.
- Password collection or automated login.
- Public posting or other irreversible external action.
- AgentCore runtime, gateway, identity or browser integration.

Those capabilities are planned in [`MULTI_TENANT_AGENT_PLATFORM.md`](MULTI_TENANT_AGENT_PLATFORM.md).

## Apply

Extract the drop-in archive into the repository root, then run:

```powershell
py -m pytest backend\tests -q
py scripts\build_web.py

git add .env.example README.md backend db deploy docs scripts site\app.html
git commit -m "Add durable multi-tenant Maker task platform"
git push origin main
```

Run the full **Deploy Brass Tacks** workflow. A frontend-only deployment is insufficient because this version adds SQS, Step Functions, Lambda handlers, IAM policies and database schema.

## Database migration

Apply before shifting traffic:

```powershell
py db\migrate.py --schema-only
```

The Lambda code also contains an additive idempotent schema bootstrap as a safety net.

## SES test setup

Verify the sender identity in Amazon SES in `us-east-1`. While the SES account is in the sandbox, also verify `virtual.icfd@gmail.com`.

Set these SSM values:

```text
/brasstacks/MAKER_EMAIL_ENABLED=true
/brasstacks/MAKER_EMAIL_FROM=<verified sender>
/brasstacks/MAKER_REVIEW_EMAIL=virtual.icfd@gmail.com
```

If email is disabled or not configured, Maker still completes and stores the draft; the notification tool records a skipped receipt.

## Minimum acceptance flow

```text
Do it
→ one CockroachDB task
→ one SQS dispatch
→ one Step Functions execution
→ one Maker atomic claim
→ one draft
→ one SES review receipt
→ exact task opens from email link
→ reconciliation creates no duplicate
```
