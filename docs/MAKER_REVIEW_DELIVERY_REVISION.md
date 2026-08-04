# Maker Review, Email Delivery, and Draft Revision

## Purpose

This change turns Maker's output from a long internal Markdown package into a production-shaped owner workflow:

```text
Maker working artifact
        ↓
Structured review workspace
        ↓
Concise owner notification
        ↓
Delivery timeline and revision history
```

The owner sees the smallest next decision needed to move the work forward. The detailed working draft remains available in Brass Tacks, but it is not dumped into email or presented as one undifferentiated wall of text.

## Root cause addressed

The earlier implementation used one generic Markdown artifact for three different jobs:

1. Maker's complete internal work package.
2. The owner-facing review experience.
3. The SES email body.

That caused overly long email messages, many placeholders, mixed instructions and final copy, and no reliable distinction between “needs owner input” and “ready to use.” It also made draft revision behave like a new independent artifact instead of a controlled version of the same task.

## Priority 1 — Professional Maker review workspace

### Structured Maker output

Maker now returns a validated package with these concepts:

- `title` — concise deliverable title.
- `summary` — short owner-facing explanation of what Maker prepared.
- `review_state` — `needs_owner_input` or `ready_for_review`.
- `owner_action` — one explicit next step for the owner.
- `owner_questions` — at most three missing decisions.
- `artifact_type` — the kind of deliverable being prepared.
- `sections` — at most five organized sections for the review workspace.
- `body` — the complete Markdown working artifact retained for detailed inspection.
- `revision` and `parent_artifact_id` — version lineage.

The Maker prompt now optimizes for usability rather than maximum length. It must:

- produce the smallest owner-ready package that moves the recommendation forward;
- ask for missing information instead of generating a giant placeholder template;
- keep owner questions to three or fewer;
- keep the review workspace to five or fewer sections;
- separate owner decisions, ready-to-use copy, and operating instructions;
- never send or publish anything itself.

### Review workspace

The task view now provides:

- a concise summary;
- the exact next owner action;
- missing-information questions;
- structured sections;
- a collapsed complete working draft;
- copy and revision controls;
- exact email receipt and delivery timeline.

The full artifact remains durable in CockroachDB/S3, while the owner-facing workspace emphasizes only what needs attention now.

## Priority 2 — Email content and delivery tracking

### Concise email notification

The SES tool no longer sends the entire Markdown artifact. It renders a dedicated email with:

- a clear subject;
- what Maker prepared;
- one next action;
- up to three questions when input is missing;
- one button back to the exact Brass Tacks task;
- an explicit statement that nothing has been published or sent to customers;
- task and revision receipts.

The exact rendered subject, plain-text body, and HTML body are stored in `tool_execution.input_data`, so the operator can inspect precisely what was sent.

### SES configuration set and events

The deployment creates an Amazon SES configuration set with an EventBridge event destination. Maker includes that configuration set and tenant-safe message tags in every SES send request.

The EventBridge worker records immutable provider events in CockroachDB:

- sent;
- delivered;
- opened;
- clicked;
- bounced;
- complaint;
- delivery delayed;
- rejected;
- rendering failed;
- subscribed.

Each event is correlated to the original tool execution and task using the SES message ID and custom tags. Provider event IDs are unique, so duplicated EventBridge delivery does not create duplicated timeline entries.

### Delivery semantics

The UI distinguishes these states:

```text
Accepted by SES
→ Delivered to recipient mail server
→ Opened
→ Link clicked
```

“Accepted” means the SES send request succeeded. It is not presented as proof that the recipient's mail server accepted the message. Delivery, bounce, complaint, open, and click are shown only after corresponding SES events are recorded.

Open and click events may be absent because email clients can block or proxy tracking. Provider events are useful operational evidence, not an absolute statement about human attention.

### Email-event schema

The additive `email_event` table records:

- provider event ID;
- SES message ID;
- tool execution, task, and business IDs;
- event type and time;
- recipient;
- clicked link when present;
- complete bounded event metadata.

The workflow projection includes a bounded event timeline for each email tool receipt.

## Priority 3 — Draft revision through chat

### Owner experience

The owner may ask in the recommendation chat:

- “Make it shorter.”
- “Make this more professional.”
- “Use a warmer tone.”
- “Remove the discount.”
- “Revise the draft for weekdays only.”

The UI also provides quick revision controls. A revision command creates a new version of the same Maker task rather than an unrelated recommendation or duplicate task.

### Deterministic action path

Clear revision requests use a deterministic action route:

```text
Owner revision request
        ↓
Authenticated tenant and accepted recommendation check
        ↓
Completed Maker task reset to queued
        ↓
Revision number incremented
        ↓
Previous artifact retained as parent
        ↓
Maker creates full replacement draft
        ↓
Previous artifact marked superseded
        ↓
New review email sent for the new revision
```

The action itself uses no LLM tokens to decide whether the owner requested a revision. Maker uses the model only when generating the replacement artifact.

### Version and idempotency rules

- Each revision has a monotonically increasing revision number.
- The prior artifact remains durable and becomes superseded only after the replacement is stored.
- S3 keys are revision-aware.
- Email idempotency keys include task ID and revision.
- Retry attempt count is reset for the newly queued revision.
- Duplicate queue deliveries remain safe because Maker must atomically claim the task.

## Multi-tenant and security boundaries

Every operation is scoped to the authenticated business:

- the browser cannot supply a different tenant ID;
- chat revision validates the session's business and find;
- SES recipient resolution remains server-side;
- the model cannot choose the sender or recipient;
- email tags carry opaque task/business/tool IDs, not customer content;
- the full working artifact is not exposed in email unless explicitly copied by the owner.

## Deployment changes

This version adds or updates:

- structured artifact columns in CockroachDB;
- `email_event` table and indexes;
- SES configuration set;
- SES EventBridge event destination;
- SES event ingestion Lambda;
- Maker Email Lambda configuration-set environment value;
- EventBridge permissions and target;
- workflow projection for artifacts, email receipts, and delivery events.

Apply the schema before shifting traffic:

```bash
python db/migrate.py --schema-only
```

Then run the full SAM deployment. A frontend-only deployment is insufficient.

## Live acceptance test

Use one brand-new recommendation.

1. Press **Do it** once.
2. Confirm one durable task appears.
3. Confirm Maker creates one structured artifact.
4. Inspect the review workspace:
   - summary is concise;
   - one next action is visible;
   - full draft is collapsed;
   - no giant raw Markdown email is shown.
5. Confirm the email tool stores:
   - From;
   - To;
   - subject;
   - exact plain/HTML content;
   - SES message ID.
6. Confirm the recipient receives the concise email.
7. Wait for the timeline to show **Delivered** when SES reports it.
8. Open the email and click the task link; verify open/click events when the mail client permits tracking.
9. In chat, type “Make it shorter and more professional.”
10. Confirm revision 2 is queued, generated, and becomes current.
11. Confirm revision 1 remains visible as superseded.
12. Confirm exactly one new email receipt exists for revision 2.
13. Wait through reconciliation and verify no duplicate draft or email is created.

Expected relationship:

```text
1 approved recommendation
→ 1 durable task
→ revision 1 artifact + email receipt
→ 1 owner revision instruction
→ revision 2 artifact + email receipt
→ one current artifact, complete immutable history
```

## Current limitations

- Existing historical emails cannot gain delivery/open/click events retroactively.
- Event publishing is operational telemetry, not guaranteed proof of human attention.
- Open/click tracking depends on recipient-client behavior.
- The current workflow prepares and reviews owner-ready work; external customer-facing publishing remains approval-gated future Executor functionality.
