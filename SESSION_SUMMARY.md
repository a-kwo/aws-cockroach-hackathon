# SESSION SUMMARY

**Brass Tacks** — AWS + CockroachDB hackathon. Written 2026-07-28. Submission deadline **August 18**.

Repo: `github.com/a-kwo/aws-cockroach-hackathon` (currently **private** — must be public before submission).

---

## What the product is

A team of AI agents that works overnight for a small-business owner. Each night it reads
reviews, competitor menus and local trends; searches everything it has ever learned about
that business; and proposes one revenue move with a dollar figure attached. Weeks later it
checks whether that move actually paid and writes the verdict to a permanent ledger —
including the failures.

The reason this can win: it is **impossible without persistent memory**. The Meter judges
predictions made on earlier nights by agent runs that no longer exist. A stateless advisor
can give advice forever and never be wrong, because nothing it said was written down.

---

## Infrastructure — all verified working, not assumed

| Component | State |
|---|---|
| CockroachDB | Cluster `brasstacks`, **serverless on AWS us-east-1**, v26.2.1 |
| Schema | 9 tables applied; vector indexes live on `observation` and `business_fact` |
| Vector search | Proven on the real cluster: cosine index, correct nearest-neighbour ordering |
| Embeddings | Amazon Bedrock, Titan Text Embeddings V2, 1024 dims (matches `VECTOR(1024)`) |
| Reasoning | Anthropic API, `claude-opus-5` |
| AWS account | `881550374737`, region us-east-1 |

### The Bedrock constraint (important, and disclosed in the README)

AWS **cannot grant this account any current Claude model**. Verified rather than assumed:
`agreementAvailability: NOT_AVAILABLE` across us-east-1/us-east-2/us-west-2 while region,
entitlement and authorization are all green; the error directs to AWS Sales rather than
offering a use-case form; and the grant that did arrive was for two **retired** models that
return `ResourceNotFoundException`. 27 non-Anthropic Bedrock models invoke fine, so nothing
is wrong with Bedrock, the credentials or the region.

Consequence: reasoning runs on the Anthropic API, embeddings stay on Bedrock. Model calls
sit behind a provider interface, so a future grant is a config change.

**Silver lining:** the MCP connector is a first-party Anthropic API feature and is *not*
available on Bedrock. Being forced off Bedrock is what makes the planned Ask agent — which
queries the live cluster read-only over the CockroachDB Cloud MCP Server — possible at all.

---

## Backend — built, tested, running

**165 unit tests** (offline, no credentials needed) + **46 integration tests** (real cluster).

```
backend/src/brasstacks/
  config.py          typed settings; fails at startup with the variable named
  providers.py       Reasoner + Embedder protocols, real impls, offline fakes
  repository.py      memory-layer interface + in-memory implementation
  repository_pg.py   CockroachDB implementation — every SQL statement lives here
  finds.py           validates model output before it becomes a stored prediction
  meter.py           verdict logic (verified / estimated / miss)
  outcomes.py        where truth about outcomes comes from
  signals.py         where Radar's observations come from
  night.py           orchestrates one night; the local end-to-end harness
  agents/radar.py    observe -> embed -> dedup -> store
  agents/analyst.py  retrieve -> reason -> validated find + evidence
  agents/meter.py    read prior predictions -> judge -> ledger
```

### Design decisions worth preserving

- **One contract suite, two implementations.** The same 46 tests run against the in-memory
  fake and the live cluster. A fake that quietly diverges from real Postgres is worse than
  no fake, because agent tests would pass against a fiction.
- **`insert_find_with_evidence` is atomic.** A recommendation without the retrieved rows
  that produced it must be impossible to create. Enforced by the database, not by a comment.
- **The Meter never invents an outcome.** `NoOutcomeSource` is the default and yields an
  ESTIMATE, so the hit rate cannot be inflated by running the Meter more often.
- **A fresh find is `proposed`, never judgeable.** Only an owner decision makes it something
  the Meter will hold us to.
- **Money is integer cents everywhere.** Floats rejected at the boundary.

---

## The data in the cluster right now

```
127 observations · 15 owner facts · 3 owner rules · 17 finds
ledger: 6 verified · 1 miss · 4 measuring  ->  86% hit rate
$126.50/day earning now, against a $8,000/month goal
```

Seeded from `db/seed/*.json` and **backdated across eight weeks**, because verify windows
are ~14 days: seeding only "today" would leave the ledger with nothing but pending
predictions, and the ledger is the most important screen in the product.

Evidence similarity is **computed, not invented** — each find's title and move embedded as
the query the Analyst would have run, scored against each cited observation.

---

## Findings that changed the build

**1. Abstract queries retrieve badly.** Measured against the corpus:

| Query | Top similarity | Right cluster? |
|---|---|---|
| "Is there unmet demand we are not serving?" | 0.238 | No |
| "Should this restaurant open for lunch?" | **0.583** | Yes |

2.5x difference, and abstract queries surfaced the *wrong* observations. The Analyst
therefore issues **six concrete hypothesis queries** per night, not one open-ended one.
This is architectural, not prompt tuning.

**2. Similarity does NOT predict outcome.** An earlier claim of mine, now corrected:

| Verdict | Top evidence |
|---|---|
| verified | 0.702 |
| **miss** | **0.315** |
| verified | 0.157 |
| verified | **0.064** |

Two verified wins have weaker evidence than the published miss. The UI must never imply
that thin evidence explains a failure. Also: 46% of all 108 evidence rows sit below 0.30,
which killed a proposed "ink density = confidence" encoding — most of the product's
receipts would have rendered at ~2:1 contrast.

**3. Live web search is harmful for this demo.** The tenant is fictional, so searching for
it returned trade-show videos and years-old market reports — 40 of 40 signals irrelevant,
polluting memory and dragging third-party trademarks into a demo where the rules bar them.
Now opt-in via `--web`. Against a real business the same source is useful.

**4. The Analyst had no memory of its own recommendations.** It proposed the same waitlist
find on nights 1 and 3. It now sees recent finds and is told not to repeat them.

**5. `now()` is the transaction timestamp in CockroachDB.** Rows written in one transaction
tied on `created_at`. Caught by the contract suite — the in-memory fake used a monotonic
clock and hid it. All audit timestamps now use `clock_timestamp()`.

**6. Models double-escape their own JSON.** A find carried the literal characters `—`
rather than an em dash and rendered as garbage. Repaired at the trust boundary in
`parse_find`. Found only by rendering the app and looking at it.

**7. The original verified/miss colours failed colourblindness.** `#177A4C` / `#B0452F`
scored ΔE 6.0 under deuteranopia (~8% of men) on the most important distinction in the
product. Validated replacement: `#118066` / `#C0442A` / `#2E5EA8` / `#A8781F` at ΔE 10.0.
**This flaw is still present in the pitch deck and should be fixed there too.**

---

## Frontend — three redesigns rejected, mock adopted

Three attempts (single-page, tabbed, and a full spec-driven rebuild called THE NIGHT DESK)
were each judged inferior to the original `Product Demo/brasstacks-jar-demo.html`.

Seeing the mock rendered finally explained why: **it is a conversation with an agent, where
the rebuilds were documents about one.** The nav is the loop with live state in each pill,
the road to the goal is an illustrated road rather than a bar chart, and its growth chart
answers the two-months-of-data problem better than the rebuild's principled refusal — one
solid bar plus ghosted dashed projections, honest and forward-looking at once.

**Current approach:** `scripts/build_web.py` treats the mock as a template and splices in
live cluster values, leaving its design untouched. Output is `web/index.html` (gitignored;
regenerate with the script).

Already live in it: coins from undecided finds, road stops with evidence counts,
earning-now, goal and shortfall, review count, and a footer naming the corpus size and
verified record.

Still the mock's scripted values: **the growth chart bars, "72% mapped", and the opening
chat message.**

The React build remains in `frontend/` but is **unused**. Its one piece worth keeping is the
long-prose logic — the mock was written for one-line finds and real Analyst output runs to
twelve lines in four distinct shapes.

`docs/DESIGN_SPEC.md` (90k chars) is the output of a 9-agent design workflow. Superseded as
a build target, but sections 1, 3 and 7 contain the data-driven reasoning above and are
worth keeping.

---

## How to run it

```bash
# tests
pytest                          # 165 unit, offline, no credentials
pytest -m integration           # 46 against the live cluster

# a night of the loop
python -m brasstacks.night --nights 3 --step 10 --accept-proposals

# refresh the frontend from the cluster
python scripts/export_fixture.py
python scripts/build_web.py
cd web && python -m http.server 8900
```

Setup gotchas that will otherwise cost a judge time, and belong in the README:

- `sslmode=verify-full` needs the cluster CA file; download from
  `https://cockroachlabs.cloud/clusters/<cluster-id>/cert`
- `sslrootcert=system` **fails on Windows** — psycopg's bundled libpq does not resolve it
  to the Windows trust store
- Edit `.env` with `python scripts/env_file.py set KEY value`, never by hand. Hand-editing
  destroyed the connection string once, including a generated password that existed nowhere
  else. `python scripts/provision_db_user.py` rotates and rebuilds it if that happens again.
- CLI output needs `PYTHONIOENCODING=utf-8` on Windows — every find carries an emoji

---

## Not done yet

**Frontend**
- Growth chart, "72% mapped", and chat greeting still on scripted values
- The demo tenant is too settled: 1 undecided find and 1 saved, so the jars look empty and
  the road has 3 stops instead of 5. Seed more open finds — this is a data problem, not UI.
- Mobile unverified

**Backend / infra**
- Nothing deployed to AWS yet: no Lambda, EventBridge, S3, API Gateway, no IaC
- The Ask agent over the CockroachDB MCP Server — the second required Cockroach tool at
  runtime — is designed but not built
- `ccloud` setup automation exists ad hoc; not yet a committed reproducible script

**Submission**
- README, architecture diagram, setup instructions
- CockroachDB tool disclosure (Vector Indexing, MCP Server, ccloud CLI) and AWS service
  disclosure (Bedrock, Lambda, EventBridge, S3, API Gateway)
- Provenance disclosure: `Product Demo/` predates this project and was built for an
  abandoned competition
- Strip all Gemini / XPRIZE branding from the deck
- Flip the repo public and confirm GitHub detects the MIT licence
- Demo video under 3 minutes, showing the memory layer working
- **Still on root AWS credentials** — swap to an IAM user before writing deploy code

---

## Suggested order from here

1. Seed more undecided finds so the demo has something to show
2. Wire the remaining scripted values in the mock to real data
3. Deploy: container-image Lambdas (psycopg needs manylinux wheels), EventBridge, API Gateway
4. Ask agent over MCP — the second CockroachDB tool
5. README and disclosures
6. **Feature freeze Aug 14**, record the video, submit Aug 17

Projects lose by building until the deadline and never recording.
