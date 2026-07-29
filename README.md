# Brass Tacks

**An agent with a track record.** A team of AI agents works overnight for a small
business: it reads the reviews, the rivals and the local trends, proposes one revenue
move with a dollar figure attached, and then — weeks later — checks whether that move
actually paid. Verdicts go on a permanent ledger, **including the failures**.

Built for the AWS + CockroachDB hackathon.

The claim this rests on: the Meter judges predictions made on earlier nights by agent
runs that no longer exist. A stateless advisor can give advice forever and never be
wrong, because nothing it said was written down. This writes it down first.

---

## See it in 30 seconds, with no credentials

The demo data is committed, so you do not need AWS keys or a database to look at the
product:

```bash
python scripts/build_web.py       # renders web/ from the committed fixture
cd web && python -m http.server 8901
```

Then open **http://127.0.0.1:8901** — landing page, with the dashboard at `/app/`.

Worth clicking, in this order:

1. **Ledger** — every call and how it turned out. The espresso row is a published miss:
   predicted +$12.00/day, actual $0.00.
2. **Radar** → click a stop → expand **"Read 127 memories · 6 questions asked"**. Those
   are the real queries the Analyst issues and the real rows the vector search returned,
   with their cosine similarities.
3. **Autopilot** → accept a find. The verified figure does not move; only the *predicted*
   line does. That distinction is the product.

## Where things live

```
site/                the frontend source — landing.html and app.html
scripts/build_web.py splices live cluster data into both, writes web/
web/                 build output (gitignored — never edit, always regenerate)

backend/src/brasstacks/
  agents/radar.py    observe -> embed -> dedup -> store
  agents/analyst.py  retrieve -> reason -> a validated find with its evidence
  agents/meter.py    read prior predictions -> judge -> the ledger
  repository_pg.py   every SQL statement in the project
  meter.py           verdict logic; finds.py validates model output

db/schema.sql        9 tables; db/seed/ the reproducible demo corpus
db/fixtures/         the exported demo tenant the site build reads

PRODUCT.md           who this is for and what must never be fabricated
DESIGN.md            the visual system, with the rules and the reasons
Product Demo/        the pre-project mock. Frozen. See Provenance below.
```

## Running it for real

Needs a CockroachDB Cloud cluster, AWS credentials for Bedrock embeddings, and an
Anthropic API key. Copy `.env.example` to `.env` and fill it in.

```bash
python db/migrate.py                       # apply the schema
python scripts/seed.py --reset             # ~150 embedding calls, a couple of minutes
python -m brasstacks.night --nights 3      # run the loop
python scripts/export_fixture.py           # refresh db/fixtures/demo.json
python scripts/build_web.py                # rebuild the site
```

## Tests

```bash
pytest                  # 194 unit tests, offline, no credentials needed
pytest -m integration   # 46 more, against a live cluster
```

The unit suite must stay green with no cloud account. `backend/tests/test_site_build.py`
asserts the honesty invariants: only verified money reaches the headline figure, at most
one projected month, an estimate is never labelled "Actual", and a miss always survives
into the view model.

## Setup gotchas that will otherwise cost you an hour

- `sslmode=verify-full` needs the cluster CA. Download it from
  `https://cockroachlabs.cloud/clusters/<cluster-id>/cert`.
- **`sslrootcert=system` fails on Windows** — psycopg's bundled libpq does not resolve it
  to the Windows trust store. Point at the downloaded file instead.
- Edit `.env` with `python scripts/env_file.py set KEY value`, never by hand.
- CLI output needs `PYTHONIOENCODING=utf-8` on Windows; every find carries an emoji.

## Disclosures

**CockroachDB is the memory layer, and it is load-bearing.**

| Tool | What the agent actually does with it |
|---|---|
| Distributed Vector Indexing | Radar embeds every observation; the Analyst runs six semantic searches over the whole corpus before proposing anything. `VECTOR(1024)` with a `business_id` prefix. |
| Cloud Managed MCP Server | *Planned, not yet built.* The Ask agent will answer owner questions by querying the cluster read-only over MCP. |
| ccloud CLI | Cluster provisioning, SQL user creation, network config. |

**AWS**

| Service | Role |
|---|---|
| Bedrock | Titan Text Embeddings V2 generates every vector in the index. No embeddings, no retrieval, no memory. |
| Lambda / EventBridge / S3 / API Gateway | *Planned.* The nightly schedule and agent runtime. |

**Reasoning runs on the Anthropic API, not Bedrock.** This is forced, not preferred.
AWS could not grant this account any current Claude model: `agreementAvailability` is
`NOT_AVAILABLE` across three regions while region, entitlement and authorization are all
green, and the grant that did arrive was for two retired models that return
`ResourceNotFoundException`. 27 non-Anthropic Bedrock models invoke fine, so nothing is
wrong with Bedrock, the credentials or the region. Embeddings stay on Bedrock, so
retrieval is AWS end to end. Model calls sit behind a provider interface, so a future
grant is a config change.

**Rosa's Trattoria is a fiction.** The corpus is hand-written so the demo is
reproducible and carries nobody's real reviews. Every number rendered by the site is
queried out of the cluster, not typed into the markup. There are no real customers,
testimonials, pricing, benchmarks, peer data or P&L, and `PRODUCT.md` records those
absences so future work does not invent them.

## Provenance

`Product Demo/` contains a pitch deck and a clickable front-end mock built **before this
project began**, for a different competition that was abandoned. The mock was generated
with AI and then refined by hand.

**The shipped frontend descends from that mock.** After three redesigns were tried and
rejected, `Product Demo/brasstacks-jar-demo.html` was copied to `site/app.html` and
rebuilt from there: its data now comes from CockroachDB, its invented panels were
replaced, and a Ledger screen was added — but its layout, CSS and interaction model
are descended from that file. `Product Demo/` itself is frozen and is never a build
input; the build reads `site/`.

An abandoned React rebuild ("The Night Desk") lived at `frontend/` and was **deleted** —
it was the third rejected redesign and only caused confusion about which directory was
the product. It remains in git history.

## Licence

MIT. See `LICENSE`.
