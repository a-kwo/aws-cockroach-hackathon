# Archive

Exports of tenants that no longer exist in the cluster. Not build inputs —
`scripts/build_web.py` reads `db/fixtures/demo.json` and never looks here.

## `rosas-tenant-2026-08-01.json`

The seeded Rosa's Trattoria demo tenant, exported immediately before it was
deleted on 2026-08-01 to make room for a real business signing up through the
onboarding endpoint.

445 rows: 1 business, 3 owner rules, 15 business facts, 127 observations,
28 finds, 178 evidence rows, 1 artifact, 9 ledger entries, 83 agent runs.
Embeddings are dropped — they are regenerable from the content and would have
made the file forty times larger.

Kept for one reason. Among those 83 agent runs are the four nights of
2026-07-29 through 2026-08-01, which the EventBridge schedule fired **unattended**
— one radar/analyst/maker/meter run and one find per night, with nobody watching.
That is the evidence for the claim the whole product rests on, that the loop is
autonomous rather than a button, and after the delete these rows existed nowhere
else. The matching CloudWatch log streams under
`/aws/lambda/brasstacks-NightFunction-*` survive independently and corroborate it.

Rosa's Trattoria is fictional, as `README.md` has always said: the corpus was
hand-written so the demo was reproducible and carried nobody's real reviews.
Nothing in this file describes a real business or a real customer.
