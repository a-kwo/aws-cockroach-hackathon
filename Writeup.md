## Inspiration

Every AI tool will give a small business owner advice all day long. None of them ever
find out whether the advice was any good — and neither does the owner.

That asymmetry is the whole reason this project exists. A chatbot can advise you for a
year and never be wrong, because nothing it said was ever written down. It has no
yesterday. Ask it in March what it told you in January and it will cheerfully invent a
new answer, with the same confidence it had the first time.

We wanted to build the opposite: an agent that commits to a number **before** the
outcome is known, and then comes back to be graded on it. Not "here are some ideas for
your restaurant," but "raise the tiramisu from $7 to $9, I think that's worth $23 a day,
check me on the 14th" — followed, two weeks later, by an honest verdict.

The moment we sketched that, the architecture wrote itself. An agent that can be held to
a prediction needs somewhere durable to put the prediction, and the reasoning that
produced it, and the evidence that reasoning drew on. The memory layer stopped being
storage and became the product. That is what pointed us at CockroachDB, and it is why we
entered this hackathon rather than a general AI one: without a real database doing real
work, there is no version of this idea that functions.

## What it does

Brass Tacks is a team of agents that works overnight for one small business.

**Radar** goes out and observes — customer reviews, competitor prices and menus, local
demand signals. Everything it finds is cleaned, deduplicated by content hash, embedded,
and written into CockroachDB as vector memory.

**Analyst** searches that accumulated memory and proposes concrete revenue moves, each
with a dollar figure and a date attached. Every recommendation stores the exact rows the
vector search returned, their cosine similarity and their retrieval rank — so a
recommendation is never a floating opinion, it is a claim with its evidence stapled to it.

**Refuter** then tries to knock the proposal down before the owner ever sees it. It is
prompted to prove the find wrong, not to review it, and it can demote a claim (strip its
dollar figure) or withhold it entirely — but only if it can cite the specific observation
that contradicts it. Prove it or price it.

**Maker** does the work. When the owner approves a move, it drafts the actual
deliverable — the menu card, the post, the note to customers — and emails it for review.

**Meter** is the one that makes this different from a chatbot. It reads predictions made
on *earlier* nights, by agent runs that no longer exist, compares them against what
actually happened, and writes a permanent verdict: verified, estimated, or miss.

**Ask** answers the owner's questions by querying the live cluster itself over
CockroachDB's Managed MCP Server, and records the SQL it ran.

**Quartermaster** handles supplies — standing orders, stock thresholds, and real
purchases through a payment seam, under spend rules that a language model cannot argue
with.

The result is a board with a track record on it. Our demo ledger reads six verified, one
miss, 86% hit rate. The miss is an espresso idea the agent got wrong — predicted +$12.00
a day, delivered $0.00 — and it stays on the board permanently. Showing the failures is
not a concession we made to honesty; it is the feature. A track record with the losses
edited out is just marketing.

## How we built it

**CockroachDB is the memory layer, and nothing else is.** Every observation lives in a
`VECTOR(1024)` column indexed with `vector_cosine_ops`, prefixed by `business_id` so
tenant isolation happens *inside* the vector index rather than as a filter applied
afterwards. Predictions, evidence, verdicts, tasks, decisions and receipts are all rows
in the same cluster. We deliberately did not introduce a second store for vectors or
sessions — a competing vector database sitting next to CockroachDB would have undercut
the entire premise.

**The Analyst asks concrete questions, and that was a measured decision.** Our first
version issued one open-ended "what should we do?" query and retrieved the wrong
observations entirely. We tested it properly against the corpus:

| Query | Top similarity | Right cluster? |
|---|---|---|
| "Is there unmet demand we are not serving?" | 0.238 | No |
| "Should this restaurant open for lunch? Is there midday demand nearby?" | **0.583** | Yes |
| "What operational problem is costing us money?" | 0.206 | No |
| "Customers complain about waiting for a table on Saturday" | **0.560** | Yes |

A 2.5x difference, and the abstract queries surfaced the wrong evidence. So the Analyst
runs six concrete hypothesis searches every night — pricing, waits, hours, competitors,
reputation, trends — and reasons over the union. That is an architectural requirement in
our codebase, not a prompt-tuning detail.

**AWS runs the whole thing.** Amazon Bedrock generates every vector the index holds
(Titan Text Embeddings V2). Nineteen container-image Lambdas are the agent runtime.
EventBridge Scheduler fires the nightly loop, which is what makes this an agent rather
than a button. S3 holds Maker's deliverables and the board; CloudFront serves it. SQS
FIFO and Step Functions make the Maker durable — CockroachDB stays the source of truth,
and a worker must atomically claim a task row before spending a single model token, so
duplicate delivery costs nothing. SES emails completed drafts and reports its own
delivery events back into the database. KMS envelope-encrypts OAuth tokens, bound to the
tenant by encryption context.

**Test-driven, throughout.** Write the test, watch it fail, write the minimum code. Every
external boundary — Bedrock, CockroachDB, Tavily, Yelp, Stripe, SES, Google — has an
injectable fake, so the full unit suite runs offline with no cloud account at all: **1,842
tests in 26 seconds**. Integration tests are marked separately and never mixed in. Money
is integer cents everywhere, never floats, and that is tested — the browser never
multiplies by 100, because `12.34 * 100` is `1233.9999999999998` in JavaScript and that
is exactly how a cent goes missing.

**The honesty rules are executable.** A set of tests enforces what the UI is allowed to
claim about the owner's money: only verified money reaches the headline figure, at most
one dashed projection and it must name the finds it depends on, an estimate is labelled
*Modelled* and never *Actual*, nothing the owner clicks can increase the verified record,
and similarity is shown as a number rather than as colour intensity. One of those tests
checks that the "projected, not earned" colour actually *wins* the CSS cascade — because
an earlier version only checked that the rule existed.

## Challenges we ran into

**AWS would not grant this account access to any Claude model on Bedrock.** We verified
it rather than assuming: `agreementAvailability: NOT_AVAILABLE` across three regions
while region, entitlement and authorization were all green, and the grant that eventually
arrived was for two *retired* models that return `ResourceNotFoundException` on invoke.
Twenty-seven non-Anthropic Bedrock models invoked fine, so nothing was wrong with
Bedrock, our credentials, or the region. On day one we moved reasoning to the Anthropic
API and kept embeddings on Bedrock — so retrieval is AWS end to end — and put every model
call behind a provider interface so a future grant is a config change. It is disclosed
plainly in our README, because a judge discovering it unstated is far worse than reading
it up front.

**A real business signed itself up, and broke our scope cut.** We had deliberately
dropped accounts and multi-tenancy on the reasoning that one seeded tenant proves the
memory claim just as well. That held right up until someone real completed onboarding on
the deployed endpoint — and a product that lets you create a profile and then shows you
somebody else's restaurant is not a product. Multi-tenancy came back in: per-business
logins with scrypt-hashed passwords, sessions where only the token *hash* is stored, and
a nightly loop that runs for every active tenant.

**Our agent was reading closed storefronts.** The nightly schedule originally fired at
06:00. In practice EventBridge fired it at 06:28, 08:07, 08:40 local — so every
observation was captured while the business was shut. "Not available right now" reached
the Analyst as an outage, and it published a find based on that misreading. We moved the
run to 18:00, inside trading hours for a restaurant, a shop and a salon alike. The owner
still wakes up to finished work.

**A quoting bug took the entire stack down.** `sam deploy --parameter-overrides` takes
one argument containing every key-value pair and re-splits it itself — so the shell's
quotes, stripped before SAM ever sees them, protect nothing. `cron(0 18 * * ? *)` arrived
at CloudFormation as `cron(0`, EventBridge rejected it, and the stack update rolled back
and took every Lambda with it. The fix is literal quote characters inside the Python
string, and deploying is now one command that fingerprints what actually goes into the
container images and skips the rebuild when nothing changed — forty seconds instead of
eight minutes.

**Our first safety gate withheld every single find.** We built a claim-standards check to
stop the Analyst overstating what its evidence supported. It was too strict: nine out of
nine real finds were withheld and the owner's board was empty. An empty board is not a
safe board, it is a broken one. We rewrote it to *demote* rather than reject — walk the
claim down a ladder from "current state" to "pattern" to "opportunity" until it reaches a
tier the evidence actually supports, and only withhold if nothing fits.

**Three frontend redesigns, all rejected.** We tried a React rebuild, then a second
design system, then a third. Each one drifted away from what the backend could truthfully
produce. In the end we went back to a pre-existing clickable mock, wired it to real
CockroachDB data, replaced its invented panels, and added a Ledger screen. The abandoned
React build was deleted outright rather than left in the repo, because it was the only
frontend a fresh clone could start and anyone opening the project assumed the scratched
design was the product. Our README discloses that lineage in full.

## Accomplishments that we're proud of

**The Meter works, and it is the whole thesis.** It reads a prediction out of
CockroachDB that was written on a previous night by an agent run that has long since
finished, scores it against a measured outcome, and publishes a verdict that cannot be
edited from the browser. That is the memory layer doing the one thing a stateless agent
categorically cannot.

**We ship the miss.** Six verified, one miss, on a permanent ledger. Most demos hide the
failure. Ours puts it on the front page, because a track record you can only lose is the
only kind worth anything.

**Absence is never rendered as zero.** This sounds small and it is the detail we are
proudest of. A find with no measurement stores `NULL`, not `0` — we found and fixed a bug
where a `NOT NULL DEFAULT 0` column was quietly storing the *prediction* as though it
were a measurement. Token counts on old runs say "unrecorded" rather than zero. A find
with no cost estimate shows nothing rather than "$0". Everywhere the system doesn't know
something, it says so.

**1,842 tests, green, offline, in 26 seconds.** Anyone can clone the repo with no AWS
account, no database and no API key and run the entire unit suite.

**Every recommendation carries its receipt.** The Analyst's full narrowing path is
visible in the product — stored signals, six vector questions, raw matches, deduplicated
context, cited evidence, and the provider-reported token count for the run. A judge can
watch retrieval drive the reasoning rather than take our word for it.

## What we learned

**Embeddings reward concrete questions and punish abstract ones.** The 0.583-versus-0.238
result reshaped our architecture. Strategic-sounding queries are exactly the wrong thing
to ask a vector index; hypothesis-shaped ones work. We would not have found that without
measuring it.

**The interesting engineering is in the failure modes, not the happy path.** Nearly every
design decision we are pleased with is a choice about what happens when something breaks.
The Refuter fails *open* — an outage costs the dollar figures, not the board. The Coster
fails to *unknown*, never to zero, because printing "costs nothing" on every card during
an outage is worse than printing nothing. The spend authority only ever returns "allow"
or "ask" — never "reject," never "silently buy."

**A language model must not be the last line of defence on money.** Our purchase
authority does integer-cents arithmetic with no model in the loop at all, because a
spending limit a model interprets is a limit that can be argued with. Every rule is
tested against the cart *total* rather than each item's subtotal, so a covered item can't
smuggle an uncovered one in through bundling.

**Writing the honesty rules as tests changed how we built.** Once "only verified money
reaches the headline figure" is a failing test rather than a good intention, it stops
being negotiable at 2am when a chart looks better the wrong way.

**Compressing a clock is not the same as faking a mechanism.** A verified verdict needs
real elapsed time — predicted one night, scored once its window closes days later — which
no live demo can wait out. So our seeded tenant has backdated history with elapsed
windows. The embeddings are real Titan vectors, the similarities are computed against
them, and the Meter genuinely reads each prediction back out of the database and scores
it. Only the calendar is stood in for, and we say so plainly rather than letting anyone
discover it.

## What's next for BrassTacks

**Wire the Coster into every night.** It is built and thoroughly tested — it prices what
a move *costs* beside what it earns, and its prompt deliberately cannot see the revenue
figure so cost can never anchor on payoff. It runs today only when passed in explicitly.
Wiring it into the deployed nightly run means every recommendation arrives with a payback
period rather than gross revenue alone, which will expose the finds worth $23 a day that
take forty days to break even.

**Harden the production boundary.** Right now authentication is enforced entirely in
handler code — carefully, and with no cross-tenant reference we could find — but API
Gateway itself has no managed identity boundary in front of owner routes, and the Ask
agent's tenant scoping is instructed rather than enforced in SQL. Before this holds any
real business's revenue figures at scale, both become hard boundaries: a managed
authorizer at the edge, and tenant scoping enforced at the query layer rather than by
prompt.

**Move SES to production access.** The delivery pipeline is wired end to end and verified
— drafts send, and delivery events flow back into the database as immutable receipts —
but the account is still in the SES sandbox, so mail only reaches verified addresses. A
new owner today gets a recorded `skipped` receipt instead of an email.

**Watch the machinery.** There are no CloudWatch alarms yet on dead-letter depth, Lambda
errors or workflow failures, and nothing consumes the dead-letter queue. A stalled Maker
pipeline would currently accumulate quietly. That is the first operational gap we would
close.

**Let the ledger accumulate for real.** Everything above serves one goal: running long
enough, for enough businesses, that the track record stops being seeded history and
becomes months of genuine verdicts. The product's entire claim is that it can be checked.
The most valuable thing we can do next is let it be checked, in public, over time —
including the times it turns out to be wrong.
