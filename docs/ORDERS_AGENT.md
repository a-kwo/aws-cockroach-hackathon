# The Quartermaster — an ordering agent for Brass Tacks

> Status: **design only.** Nothing in this document is built. Written 2026-08-07.
> Supersedes nothing. The phasing in §11 is the part to argue with.

An agent that places and tracks the business's **own supply orders**, starting with
DoorDash, and — later, when access allows — reads the business's **incoming** DoorDash
sales.

---

## 1. Why this belongs here, and not in a different project

The honest test for any addition is whether it serves the core claim: *an agent that
remembers what it predicted and checks whether it was right.* Most candidate features
fail that test. This one passes it for a specific and slightly surprising reason.

`db/schema.sql` says it out loud, in the comment above `find_outcome`:

> Without this table `NoOutcomeSource` is the only outcome source that exists, every
> verdict the Meter can ever reach is ESTIMATED, and the published hit rate is
> permanently undefined.

Today a **verified** verdict requires the owner to type in what a move earned. That is
the correct design — a move is measured when the person who runs the business says what
it earned — but it means the Meter's strongest output depends on manual data entry that
most owners will never do. The ledger degrades to a wall of *Modelled*.

Procurement breaks that deadlock, because **a supply order produces its own receipt**.
Line items, quantities, integer cents, a timestamp, a provider reference. No owner data
entry, no estimation, no model in the loop. If the night predicts *"consolidating your
three emergency runs into one Tuesday order saves $23/day"*, the next fortnight of
receipts either shows that or it doesn't.

That is the first outcome source in this system that the machine can produce by itself.
It is worth building for that reason alone, and the ordering convenience is a bonus.

**The corollary is a constraint, not a bonus:** spend that Brass Tacks itself initiated
is the only spend it may count. An outcome derived from an order the agent placed is
clean. An outcome derived from guessing at orders the owner placed elsewhere is
estimation wearing a receipt's clothing, and §10 forbids it.

---

## 2. What DoorDash actually offers (as of 2026-08-07)

Researched, not assumed. The single most important fact is that the shiny new thing and
the useful thing are at opposite ends of the pipe.

| Surface | Side | What it does | Access |
|---|---|---|---|
| **`dd-cli`** | Consumer | Search stores, browse menus, build cart, check out | Waitlist beta, **macOS arm64 only** |
| **Marketplace API** | Merchant | Receive orders, menu pull, order adjustment | **Closed** — pipeline at capacity, backlog reviewed quarterly |
| **Reporting API** | Merchant | Sales reports into your own warehouse | Gated to merchant developers / preferred partners |
| **Drive API** | Merchant | Delivery-as-a-service for your *own* ordering channel | **Self-serve today** (`developer_id` / `key_id` / `signing_secret`, JWT) |

`dd-cli` was announced 2026-07-15 by DoorDash's CTO and is explicitly built to be driven
by coding agents with shell access. It emits `--json` on every command, so parsing is
not the problem.

**Third-party "DoorDash MCP servers" are out of scope and should stay out.** The ones on
GitHub drive Playwright against DoorDash's internal GraphQL API with a logged-in session.
That is unsanctioned, brittle against any UI change, and would put scraped session
cookies into a public hackathon repo. `dd-cli` is the sanctioned path and we take it.

### The three blockers, stated plainly

1. **macOS-only.** Brass Tacks runs on Lambda container images, which are Linux. There is
   no `dd-cli` binary that can run in the nightly loop. This is not a packaging problem
   to be worked around; the artifact does not exist. §8 is the answer.
2. **Waitlist-gated.** Full function needs an approved account and an interactive
   `dd-cli login`. A judge cloning the repo cannot reproduce it. §11 Phase 0 is the answer.
3. **It spends real money from a real payment method.** An autonomous nightly Lambda with
   checkout authority is a bad idea in general and an indefensible one in a demo. §10 is
   the answer.

---

## 3. What we are actually building

Three integration points, deliberately separate, because they have completely different
risk profiles and only the third one can lose money.

```
                    ┌──────────────────────────────────────────┐
   READ (safe)      │ DoorDashSignalSource                     │
                    │   rival menus, prices, promos, hours     │
                    │   → RawSignal → Radar → embeddings       │
                    │   → observation rows → Analyst retrieval │
                    └──────────────────────────────────────────┘

                    ┌──────────────────────────────────────────┐
   CREDENTIALS      │ external_connection(provider='doordash')  │
                    │   per-tenant, encrypted, already exists   │
                    └──────────────────────────────────────────┘

                    ┌──────────────────────────────────────────┐
   WRITE (gated)    │ work_task(agent='quartermaster')          │
                    │   approval_state='pending' → owner → ok   │
                    │   → tool_execution('doordash.place_order')│
                    │   → receipt → ProcurementOutcomeSource    │
                    │   → Meter → VERIFIED                      │
                    └──────────────────────────────────────────┘
```

**Almost none of this is new infrastructure.** The control plane already has everything
the write path needs, and that is the strongest argument for the design being right:

- `work_task.approval_state` (`not_required` / `pending` / `approved` / `rejected`) and
  the `waiting_user` status are already the human gate.
- `work_task.idempotency_key` (unique) already collapses a double-click into one order.
- `tool_execution` already carries its own idempotency key and an `external_reference`
  for the provider's receipt, under a comment that reads *"Models never hold credentials;
  the deterministic tool adapter performs the action and records the provider's
  reference here."* That comment was written for the Maker and describes this exactly.
- `external_connection` already stores per-tenant provider credentials with encryption
  and a status lifecycle. DoorDash is a new `provider` value, not a new table.
- `decision_event` already gives the append-only record of who approved what.

The genuinely new pieces are: one `SignalSource`, one agent module, one tool adapter, one
`OutcomeSource`, and the local worker in §8.

---

## 4. The read path — `DoorDashSignalSource`

The cheapest and safest piece, and useful on its own even if the write path is never
built.

`SignalSource` is a `Protocol` in `backend/src/brasstacks/signals.py` with `name`,
`retention_hours`, and `fetch(business_name, city, limit) -> Sequence[RawSignal]`. A
DoorDash source implements it and drops into `build_night_sources()` beside Tavily with
no other change.

What it observes — the tenant's competitive surface, from the demand side:

- Rival storefronts within the delivery radius: menu items, prices, promos, hours.
- The tenant's **own** DoorDash listing: rating, prep-time estimate, out-of-stock items.
- Supply-side stores (DashMart, grocery, restaurant-supply) for the items the tenant buys.

This is materially better Radar input than web search. Tavily returns prose about the
business; this returns *prices with numbers attached*. Per the retrieval note in
CLAUDE.md, Titan responds far better to concrete hypothesis-shaped queries, and
price-bearing observations are exactly what makes queries like *"a competitor undercuts
our lunch combo"* retrieve the right cluster.

**`retention_hours` must be set from DoorDash's terms, not from convenience.** The field
exists precisely so a licence restriction travels with the source that is bound by it —
the Yelp source already declares 24 hours for the same reason. Read the terms before
picking a number; do not default to `None`.

---

## 5. The write path — the Quartermaster

A new agent, `agent='quartermaster'`, with three task types:

| `task_type` | What it does | Spends money |
|---|---|---|
| `procurement.draft_order` | Build a cart, price it, stop | No |
| `procurement.place_order` | Check out the approved cart | **Yes** |
| `procurement.reconcile` | Pull the receipt, write line items | No |

The split is the safety boundary. Drafting is free and can run unattended; only
`place_order` touches the payment method, and it exists as a separate row with its own
approval gate and its own idempotency key.

**The nightly loop never places an order.** The most a night may do is create a
`draft_order` task. `run_night()` sequences Radar → Analyst → Maker → Meter and stays
exactly as it is; the Quartermaster is request-driven like Ask and Decision, not part of
the chain. This also keeps the night inside its time budget.

### 5.1 Four ways an order starts

Ordering is a **utility the owner has**, not only a consequence of an insight. An earlier
draft of this document had exactly one entry point — a find, then *Do it* — which was too
narrow: it meant the owner could not simply ask for tomatoes.

| Trigger | Who starts it | Example |
|---|---|---|
| `owner_instruction` | The owner, in words | *"Order 20lb of tomatoes and a case of olive oil"* |
| `standing_order` | A schedule the owner set | *"The usual produce order, every Tuesday"* |
| `stock_threshold` | The agent's read of stock | *"Tomatoes are below par"* — §7 |
| `find` | The Analyst | *"Consolidate your emergency runs — $23/day"* |

**All four converge on the same pipeline**: `draft_order` → authorization → `place_order`
→ receipt → Meter. The trigger changes *who asked* and *what authorizes it*; it never
changes the machinery, the receipt, or the safety rules. One code path to test, one
receipt format, one outcome source.

This fits the existing table without a migration: `work_task.find_id` is already nullable,
so an order that no find produced is representable today. The trigger goes in
`input_data`, and `requested_by_account_id` already records a human requester.

`owner_instruction` gets its own request-driven handler rather than being bolted onto
Ask. **Ask stays read-only over MCP** — that is a stated property of the system and worth
more than the convenience of one chat surface. Ask may later route an intent to the
Quartermaster; it may not itself place orders.

---

## 6. Autonomy — how much the agent may do alone

This is the real question behind "the owner should be able to trust the agent to make
certain purchases," and it needs a sharper answer than a yes/no toggle.

**Per item or category, the owner sets a level:**

| Level | Behaviour |
|---|---|
| `ask_always` | Draft the cart, wait for approval. **The default for everything.** |
| `ask_if_over` | Place it automatically under a cents threshold; ask above it |
| `auto` | Place within the caps, notify afterwards |

So *"reorder produce automatically up to $200/week, but ask me about anything else"* is
expressible, which is the actual shape of the trust an owner wants to give. A blanket
"the agent may buy things" is not.

### Where this lives, and why not in `owner_rule`

`owner_rule` is the right *concept* — the schema calls it "the leash you hold," and it
already carries `cap_cents`. But it stores **prose**, fed into the Analyst's and Maker's
prompts. A model reading *"you can reorder produce up to $200 a week"* and deciding
whether a given cart complies is exactly the thing §10 rule 2 forbids: a spend ceiling
must be enforced by deterministic code before checkout, not interpreted by a language
model that can be argued with.

So: **`owner_rule` stays the prose leash on reasoning. Standing purchase authority is
structured** — a `purchase_authority` row naming the item or category, the level, the
per-order and per-period caps, and the cadence for standing orders. Prose guides what the
agent thinks; structure governs what it may spend. The two must not be the same field.

### Scheduling without new infrastructure

A standing order does **not** get its own EventBridge schedule. The night already runs
daily per tenant, so it evaluates which standing orders are due and creates their
`draft_order` tasks. No per-tenant infra to provision, no drift between a schedule and
the row that describes it, and it is testable offline like the rest of the night. New
tenants get scheduling for free.

---

## 7. Knowing what is in stock

The third trigger needs something Brass Tacks does not have: a view of inventory. Three
ways to get one, and only one is tractable now.

1. **Par levels plus consumption inferred from receipts.** The owner declares what they
   keep on hand and the reorder point. The agent depletes that model using purchase
   history and elapsed time, and sharpens it as receipts accumulate. No new hardware, no
   gated API, and it gets better the longer the system runs — which is the memory layer
   doing its job.
2. **POS integration.** Real stock data, and it needs merchant-side access we do not
   have. Later.
3. **Vision on the walk-in.** Demos well, works badly. Out of scope.

Option 1, and with a caveat that matters more than the mechanism:

> **An inferred stock level is a guess, not a measurement**, and the system already has
> strong opinions about not confusing those. The ledger will not call a modelled figure
> *Actual*; a depletion estimate must not be called *stock on hand*.

Consequences: inference-triggered orders default to `ask_always` regardless of the
category's level, and the draft must show its reasoning — *"last bought 9 days ago, you
typically go through this in 7"* — so the owner can correct a wrong model instead of
discovering it in a delivery. Promoting an item to `auto` on inferred stock should require
the inference to have been right about that item several times, checked against real
receipts. Same discipline as the Meter, applied to a different prediction.

---

## 8. Where the tool adapter runs — the interesting problem

`dd-cli` is macOS-only. Lambda is Linux. The obvious answers are all bad: shipping a
Linux binary that does not exist, reverse-engineering the private API, or driving a
browser in Lambda.

The good answer is already in the schema. `work_task` carries:

```
claimed_by       STRING,
claim_token      UUID,
lease_expires_at TIMESTAMPTZ,
```

That is a lease-based claim protocol — written for SQS workers, but it does not care
what claims the row. So:

> **CockroachDB is the queue between the cloud that decides and the Mac that acts.**
> A small local worker on the owner's machine polls for `queued` Quartermaster tasks,
> claims one with a lease, shells out to `dd-cli --json`, writes the `tool_execution`
> receipt, and releases. If the Mac is asleep, the lease expires and the task returns to
> the pool. Nothing is lost and nothing is duplicated.

Two things to like about this. It is the only design that works given the platform
constraint, and it makes the memory layer *more* load-bearing rather than less: the
database is not storing the work, it is coordinating two runtimes that never talk to
each other directly. That is a better hackathon story than another Lambda would be.

The worker is deliberately dumb — claim, exec, record, release. No model calls, no
decisions. All reasoning stays in the cloud where it is tested.

---

## 9. Merchant side, for when access lands

Designed now, built when the gate opens. Kept short because speculating in detail about
an API we cannot read is how designs rot.

- **Reporting API** → a `SalesSignalSource` writing real order volume, item mix and
  daypart revenue as observations. This is the single highest-value Radar input that
  exists, because it is the tenant's actual sales rather than a proxy for them.
- **Marketplace API** → menu and price *writes*, which would let the Maker execute a
  price find directly instead of producing an artifact for the owner to apply by hand.
- **Drive API** — self-serve today, and worth a look independently: it enables a genuine
  find shaped *"your marketplace commission cost $1,840 last month; here is a direct
  ordering link on the same driver network."* That is found money with a hard number and
  a Meter-checkable outcome. It is also a bigger build than everything above combined,
  and it is a different product decision, so it is noted here and not scheduled.

---

## 10. Money safety — non-negotiable

These are product rules, not implementation details, and each should get a test that
fails loudly.

1. **Every order traces to an explicit owner authorization recorded as a
   `decision_event`** — either approval of this specific cart, or a standing authorization
   the owner created that names the item or category and its caps (§6). Granted ahead of
   time is still granted. What is never acceptable is an order authorized by nothing but
   the model's judgement that it seemed reasonable.

   > An earlier draft of this rule required per-cart approval every time. That is the
   > safest possible rule and it makes the product useless for its most common case —
   > the owner who wants produce handled without being asked weekly. The rule was
   > loosened deliberately; the thing being preserved is *traceability to a human
   > decision*, not the frequency of the clicking.

2. **A per-tenant spend ceiling per order and per rolling 7 days**, enforced in the
   adapter before checkout and re-checked against `tool_execution` history. This is code,
   not prose, and not a model's reading of prose (§6). It binds standing authority and
   per-cart approval alike: a bug in the agent, or an owner who set a generous rule and
   forgot, must not be able to empty a bank account.
3. **The draft the owner approved is the cart that is bought.** The approved cart is
   hashed into the `place_order` idempotency key; if prices moved between draft and
   checkout, the task goes back to `waiting_user` rather than silently buying at the new
   price. Under standing authority there is no cart to approve, so the same protection
   becomes a bound: a total outside the authorized range escalates to `waiting_user`
   instead of proceeding.
4. **Every autonomous order is reversible for a window.** An `auto` order notifies the
   owner immediately and offers cancellation while the provider still allows it. Trust
   the owner grants ahead of time must stay revocable at the moment it is exercised.
5. **Money stays integer cents**, per the standing rule. Receipts arrive as decimal
   strings; parse to cents at the boundary and never let a float in.
6. **Spend is not revenue.** A procurement receipt is a *cost* fact. It may verify a
   savings prediction; it may never increment the verified daily figure directly, and the
   existing UI honesty rules apply unchanged.
7. **Only Brass-Tacks-initiated orders count as outcomes** (§1). Orders the owner placed
   by other means are not receipts we are entitled to reason from.

---

## 11. Phasing

### Phase 0 — the hackathon prototype (no DoorDash access required)

The point of Phase 0 is that it is **demoable and reproducible with zero credentials**,
which the waitlist makes mandatory rather than optional. It also happens to be exactly
what the TDD rule already demands: *Bedrock and CockroachDB calls get faked at the
boundary… a contributor with no cloud account should be able to run the full unit suite
green.* The DoorDash boundary gets the same treatment.

- `DoorDashSignalSource` backed by a **committed fixture**, in the manner of
  `CorpusSignalSource` — honest about being a fixture, in the same way and for the same
  reason.
- The full Quartermaster task flow against a `FakeDoorDashTool`: draft → `waiting_user`
  → owner approves → `tool_execution` receipt → `ProcurementOutcomeSource` → Meter reads
  it on a later run → **VERIFIED**.
- **All four triggers from §5.1**, since they are the same pipeline with different
  entry points and building one is most of the work of building all four. Specifically:
  `owner_instruction` (a handler that turns words into a draft cart), `standing_order`
  (evaluated by the night, no new infrastructure), `stock_threshold` (par levels plus
  depletion inferred from receipts, always `ask_always` in this phase), and `find`.
- **The autonomy ladder and its enforcement** (§6): `purchase_authority` rows, and the
  deterministic cap check that runs before any checkout. Worth building against the fake
  adapter precisely *because* it is the code that must not be wrong when the adapter is
  real — the ceiling is easiest to test against a tool that cannot actually charge anyone.
- The local worker, running against the fake adapter.

The demo beat this buys is the one CLAUDE.md calls the money shot, and it is better than
the current version of it: *a prediction made on Monday night, an action taken Tuesday, a
receipt written Tuesday, and Wednesday's night reading that receipt back out of
CockroachDB and marking the prediction verified — with no human typing in a number.*

Flipping to real is a config change and a waitlist approval, not a rewrite. That is the
whole reason for faking at the boundary rather than mocking inside the agent.

### Phase 1 — real `dd-cli`, read-only

Live rival pricing into Radar, on the owner's Mac. No checkout. Needs waitlist access.
Low risk, immediately useful, and it validates the local worker against a real binary
before that worker is ever trusted with a payment method.

### Phase 2 — real ordering

`place_order` against live `dd-cli`, behind every rule in §10. Should not ship until
Phase 1 has run unattended for a while and the spend ceilings have been tested against a
deliberately misbehaving agent.

### Phase 3 — merchant side

Whenever DoorDash opens the gate. §9.

---

## 12. Test plan

Per the working agreement: test first, fail first, and the money math gets the unhappy
paths.

- `ProcurementOutcomeSource`: receipt with no matching find; duplicate receipt for one
  task; partial period; a receipt that arrives *before* `verify_after`; a saving that
  lands under the miss threshold and must read MISS, not VERIFIED.
- Cents parsing: decimal strings, currency symbols, thousands separators, a provider
  returning a float. No test may construct a money value from a float.
- Idempotency: the same approved cart dispatched twice produces one `tool_execution`.
- Lease expiry: a worker that dies mid-order does not double-charge on reclaim. This is
  the highest-risk path in the whole design and deserves the most tests.
- Spend ceiling: an agent that tries to exceed it fails closed and records why.
- Approval gate: a `place_order` task with `approval_state != 'approved'` must refuse to
  execute, tested directly rather than only through the handler.
- Standing authority: an order outside its named items or categories is refused; an order
  inside them but over the cap is refused; a revoked authority stops taking effect
  immediately; an expired cadence does not fire. Each of these is a way an owner ends up
  paying for something they did not agree to, so each gets its own test.
- Stock inference: a wrong depletion model must produce a *draft*, never a purchase.

---

## 13. Open questions

1. **Does the waitlist grant read-only access separately from ordering?** If yes, Phase 1
   gets much easier to justify and can ship before any payment method is attached.
2. **What do DoorDash's terms say about retaining observed rival prices?** Determines
   `retention_hours`, and it is a compliance question, not a preference.
3. **Is the tenant that would use this an actual restaurant with an actual DoorDash
   account?** The design assumes so. If the demo tenant stays fictional, Phase 0 is the
   whole story for the hackathon and Phases 1–2 wait for a real one — the same reasoning
   that already keeps the fictional corpus away from real tenants in
   `build_night_sources()`.
4. **Spend ceiling defaults.** Wants an owner's opinion, not a developer's guess.
5. **How does the owner express a standing order?** A structured form is unambiguous and
   tedious; prose parsed into structure is pleasant and can be misread. Leaning towards
   prose in, structure shown back for confirmation — the owner types it however they like
   and approves the machine-readable version, so the thing that governs spending is
   something a human explicitly signed off.
6. **Does `auto` ship at all in the first real version?** `ask_always` and `ask_if_over`
   cover most of the value at a fraction of the risk, and `auto` can wait until the
   depletion model and the caps have a track record.
