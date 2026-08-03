# Reconsidering an Owner Decision Without Rewriting History

## Status

Implemented in Brass Tacks v30.

The product rule is simple:

> **An owner may reconsider a decision, but Brass Tacks never erases what already happened.**

`Return to For You` is therefore not a destructive undo. It closes the current
decision cycle, preserves its task, draft, email and tool receipts, and opens a
new cycle in which the recommendation can be considered again.

---

## 1. Owner experience

An approved recommendation exposes **Return to For You** in two places:

- the recommendation chat drawer;
- the newest accepted event in Memory Engine's Owner decision history.

The owner selects a reason and may add a short note:

- Approved by mistake
- Timing changed
- Cost or staffing concern
- Recommendation needs revision
- Other

After confirmation, the card returns to the For You feed with a **Reopened**
label. The owner can then choose **Do it** or **Pass** again.

The confirmation text explicitly states that the original Do it, Maker task,
draft and email receipt will remain in history.

The owner may also write a direct instruction in the corresponding chat, for
example:

```text
Put this back in For You.
I approved this too quickly. Let me reconsider it.
Reopen this recommendation.
```

These commands take a deterministic zero-token path. The language model does
not decide whether to mutate workflow state.

---

## 2. Decision cycles

A single CockroachDB `find` remains the stable recommendation identity. Every
reconsideration increments its `decision_cycle`.

```text
find_123 · cycle 1
  owner.accepted
  task_A
  draft_A
  ses.send_review_email receipt

find_123 · cycle 2
  owner.reopened
  owner.accepted (or owner.passed)
  task_B, if accepted again
  draft_B, if Maker completes it
```

Cycle-aware idempotency prevents the second approval from colliding with the
first:

```text
Cycle 1 Maker task: find_123:maker_draft:v1
Cycle 2 Maker task: find_123:decision_cycle:2:maker_draft:v1
```

The first-cycle key remains backward compatible with existing tasks. Later
cycles always carry the cycle number.

---

## 3. Append-only history and current projection

Brass Tacks keeps two separate representations.

### 3.1 Immutable event history

`decision_event` is append-only. It records:

- business and recommendation;
- decision cycle;
- event type;
- previous and new status;
- authenticated actor account;
- reason code and note;
- source, such as owner button or chat;
- timestamp;
- compact transition data.

Current event types are:

```text
owner.accepted
owner.passed
owner.saved_later
owner.undo_pass
owner.reopened
```

Pre-migration accepted recommendations may not have an original event row. When
one of those is reopened, Brass Tacks first writes an explicitly marked
`legacy_projection_backfill` accepted event. It never presents an inferred
timestamp or actor as a directly observed receipt.

### 3.2 Current projection

The `find` row remains the fast current-state projection consumed by For You:

```text
status
decision_cycle
reopened_at
reopen_reason_code
reopen_reason_note
```

On reconsideration, the current projection changes from `accepted` to
`proposed`, while the earlier accepted event remains untouched.

This separation gives the owner a flexible feed and gives operators a reliable
audit trail.

---

## 4. Safe reversal policy

A workflow can only be reopened when its side effects can still be represented
honestly.

| Existing state | Reconsider behavior |
|---|---|
| Approved, task not started | Cancel/supersede the task and return the recommendation to For You |
| Task queued or retrying | Mark it cancelled and superseded; dispatch/reconciliation ignores it |
| Maker currently generating | Set cancellation/supersession state; the worker rechecks its claim before persisting output |
| Draft ready | Preserve and archive the draft as superseded |
| Review email sent only to the owner | Preserve the SES receipt; archive the cycle and reopen safely |
| Customer-facing tool running or completed | Block direct reconsideration; require a corrective task |
| Meter result already recorded | Block reopening the old move; create a new recommendation revision |

The current reversible internal review tool is:

```text
ses.send_review_email
```

A future customer-facing tool such as:

```text
google_business.publish_post
customer_email.send_campaign
pos.update_menu
```

is not reversible merely because an owner changed their mind. Brass Tacks must
create a correction, update or removal task and preserve the original action.

---

## 5. Maker cancellation and supersession

Reconsideration runs in one CockroachDB transaction with the current
recommendation lock.

For every active task from the previous cycle, it:

1. marks the task `superseded_at`;
2. cancels queued, retrying, running or waiting-user work;
3. removes its active claim and lease;
4. appends a task event explaining the owner reconsideration;
5. marks existing artifacts from that cycle as superseded;
6. opens the next decision cycle on the recommendation;
7. appends `owner.reopened`.

The Maker worker checks `task_can_continue`:

- before doing work;
- after the model returns and before storing output;
- after artifact creation and before completing the task.

This closes the race where a running Maker process could otherwise save a new
current draft after the owner reopened the recommendation.

Historical tasks, artifacts and tool receipts are never deleted. Memory Engine
labels them archived or superseded and associates them with their original
cycle.

---

## 6. Multi-tenant safety

The browser does not supply the authoritative tenant.

```text
Bearer session
  -> authenticated account
  -> account business_id
  -> tenant-scoped find lock and transition
```

Every reconsider query includes the authenticated `business_id`. An owner
cannot reopen another business's recommendation by guessing a UUID.

Operator sessions may inspect the history but cannot impersonate the owner or
press Return to For You.

---

## 7. Chat behavior and token efficiency

Reconsider commands are recognized before the general Ask-agent model call.
The handler:

1. authenticates the owner;
2. validates the recommendation and side-effect policy;
3. applies the deterministic CockroachDB transition;
4. stores the owner's message and a confirmation response;
5. records an Ask run with `0` input and `0` output model tokens.

The reason becomes owner-decision memory available to Analyst. It is not treated
as customer-demand evidence.

Example future use:

```text
Owner repeatedly reopens staffing-heavy moves
  -> relevant decision reasons retrieved
  -> Analyst lowers the rank of staffing-heavy recommendations
  -> market evidence remains the proof of demand
```

Only relevant prior decision reasons should enter an Analyst prompt; the full
decision history remains durable in CockroachDB without being resent to the
model.

---

## 8. UI contract

### For You

A reopened recommendation shows:

```text
REOPENED
Previously approved · returned for another decision
```

The same recommendation ID is retained, while the decision-cycle number
increments.

### Growth and chat

An approved recommendation's drawer shows **Return to For You**. A passed item
continues to show **Undo Pass**, which is a different transition:

- Undo Pass: `rejected -> accepted` in the same cycle;
- Reconsider: `accepted -> proposed` and opens the next cycle.

### Memory Engine

Decision history is newest first and displays each immutable event with:

- Do it, Pass, Undo Pass or Reconsider;
- cycle number;
- business owner;
- exact timestamp;
- reason and optional note;
- source;
- routing result.

Maker shows current tasks separately from archived/superseded cycles. An
archived draft remains reviewable but is visibly not the current deliverable.

---

## 9. Database additions

### `find`

```sql
decision_cycle       INT NOT NULL DEFAULT 1
reopened_at          TIMESTAMPTZ
reopen_reason_code   STRING
reopen_reason_note   STRING
```

### `decision_event`

```sql
id
business_id
find_id
decision_cycle
event_type
previous_status
new_status
actor_account_id
reason_code
reason_note
source
data
created_at
```

### `work_task`

```sql
decision_cycle
cancel_requested_at
cancelled_at
superseded_at
```

### `artifact`

```sql
decision_cycle
superseded_at
```

`db/schema.sql` is canonical. `decision_schema.py` and `task_schema.py` provide
an additive serverless bootstrap during a rolling deployment, but explicit
migration remains the preferred production process.

---

## 10. API contract

The existing owner decision route accepts:

```http
POST /v1/finds/{find_id}/decision
Authorization: Bearer <session>
Content-Type: application/json

{
  "decision": "reconsider",
  "reason_code": "needs_revision",
  "reason_note": "The price needs another review"
}
```

A successful response includes:

```json
{
  "status": "proposed",
  "previous_status": "accepted",
  "previous_cycle": 1,
  "decision_cycle": 2,
  "reopened_at": "...",
  "event_id": "...",
  "cancelled_task_ids": ["..."],
  "superseded_task_ids": ["..."],
  "superseded_artifact_ids": ["..."],
  "maker": "cancelled_or_superseded"
}
```

Repeated calls after a successful reopen return the existing reopened state
without opening another cycle.

---

## 11. Acceptance test

Use one new recommendation:

1. Press **Do it**.
2. Confirm one cycle-1 task, one draft and one owner-review email receipt.
3. Open the approved recommendation in chat or Memory Engine.
4. Press **Return to For You**, select a reason and confirm.
5. Confirm the card returns to For You with a Reopened label.
6. Confirm Memory Engine contains both `owner.accepted` and `owner.reopened`.
7. Confirm the old task and draft are archived/superseded, not deleted.
8. Press **Do it** again.
9. Confirm a new cycle-2 task and a new current draft are created.
10. Wait through reconciliation and confirm neither cycle has duplicate work.

Expected relationship:

```text
1 stable recommendation
  -> cycle 1: 1 accepted event, 1 task, 1 archived draft
  -> 1 reopened event
  -> cycle 2: 1 new owner decision, at most 1 current Maker task
```

Also verify that a recommendation with a successful customer-facing tool receipt
or a Meter result is refused with instructions to create a corrective/revision
task.

---

## 12. Future extensions

- `recommendation_revision` links for evidence refresh after a stale reopen.
- Corrective-task generation for already-published external actions.
- Owner-decision preference retrieval in Analyst scoring.
- Cancellation callbacks for external tools that support true cancellation.
- Retention and redaction policies for free-text reconsider notes.
- Operator metrics: reopen rate, common reason, cycle duration and downstream
  outcome by cycle.
