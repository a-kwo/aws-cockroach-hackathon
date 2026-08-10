"""Load the demo tenant, its observation corpus, and its backdated history.

    python scripts/seed.py              # seed (refuses if data already exists)
    python scripts/seed.py --reset      # delete the demo tenant first
    python scripts/seed.py --dry-run    # validate the JSON, embed nothing

Writes the created business id to .env as BRASSTACKS_BUSINESS_ID.

Embedding runs through Bedrock Titan, one call per observation, so a full seed
makes roughly 150 API calls and takes a couple of minutes. Inserts are batched
modestly: the CockroachDB docs warn that large batches of VECTOR values degrade
write performance, and IMPORT INTO is unsupported on vector-indexed tables, so
INSERT is the only route in.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = REPO_ROOT / "db" / "seed"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from env_file import set_key  # noqa: E402

from brasstacks.auth import hash_password  # noqa: E402
from brasstacks.config import Settings  # noqa: E402
from brasstacks.providers import build_embedder  # noqa: E402
from brasstacks.repository import EvidenceRef, cosine_similarity  # noqa: E402
from brasstacks.repository_pg import PostgresRepository  # noqa: E402

#: Small enough that a failure mid-seed leaves a comprehensible amount of work
#: to redo, and avoids the large-VECTOR-batch write penalty.
BATCH_SIZE = 25

#: Must match the observation_kind enum in db/schema.sql. Kept here so an invalid
#: kind fails in --dry-run rather than after 127 embedding calls have been paid
#: for — which is exactly what happened the first time this ran.
VALID_KINDS = frozenset({
    "review", "rival_price", "rival_menu", "trend", "social", "owner_upload",
})

#: Must match the find_status enum. Only 'accepted' and 'live' are judgeable, so
#: a seeded verdict on any other status would be silently unreachable.
VALID_STATUSES = frozenset({
    "proposed", "accepted", "later", "rejected", "live", "retired",
})
JUDGEABLE = frozenset({"accepted", "live"})


def load(name: str) -> dict:
    return json.loads((SEED_DIR / name).read_text(encoding="utf-8"))


def at_days_ago(days: int, anchor: date, hour: int = 19) -> datetime:
    """A timestamp `days` before the anchor date, during evening service."""
    return datetime.combine(
        anchor - timedelta(days=days), time(hour=hour), tzinfo=timezone.utc
    )


def validate(observations: list[dict], finds: list[dict]) -> None:
    """Fail before spending money on embeddings.

    A find citing an observation id that does not exist would abort the seed
    partway through, after the embedding bill has already been paid.
    """
    problems = []

    ids = [o["id"] for o in observations]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        problems.append(f"duplicate observation ids: {sorted(duplicates)}")

    # Content must be unique too, or dedup silently drops rows that finds cite.
    texts = [" ".join(o["content"].split()).casefold() for o in observations]
    dupe_text = {t for t in texts if texts.count(t) > 1}
    if dupe_text:
        problems.append(f"{len(dupe_text)} observation(s) share identical content")

    for observation in observations:
        if observation["kind"] not in VALID_KINDS:
            problems.append(
                f"{observation['id']}: kind {observation['kind']!r} is not in the "
                f"observation_kind enum {sorted(VALID_KINDS)}"
            )

    known = set(ids)
    for find in finds:
        if find["status"] not in VALID_STATUSES:
            problems.append(f"{find['id']}: status {find['status']!r} is not in "
                            f"the find_status enum")
        if not find.get("evidence"):
            problems.append(f"{find['id']}: no evidence")
        for ref in find.get("evidence", []):
            if ref not in known:
                problems.append(f"{find['id']}: unknown evidence {ref!r}")

        window = find["verify_window_days"]
        if find.get("ledger"):
            if find["created_days_ago"] < window:
                problems.append(
                    f"{find['id']}: has a verdict but its {window}-day window has "
                    f"not elapsed ({find['created_days_ago']} days ago)"
                )
            if find["status"] not in JUDGEABLE:
                problems.append(
                    f"{find['id']}: has a verdict but status {find['status']!r} is "
                    "not judgeable, so due_finds would never have surfaced it"
                )
            verdict = find["ledger"]["verdict"]
            actual = find["ledger"]["actual_daily_cents"]
            # An estimate is a verdict reached without a measurement, so there
            # is no actual to seed. The row here used to carry the prediction
            # back as the actual — the same thing the Meter was doing in
            # production, and the reason the column is nullable now.
            if verdict == "estimated" and actual is not None:
                problems.append(
                    f"{find['id']}: labelled an estimate but carries an actual "
                    f"of {actual} — an estimate has nothing measured behind it"
                )
            # A verified verdict with zero actual money would be scored a MISS by
            # meter.judge, contradicting the seeded label.
            if verdict == "verified" and (actual is None or actual <= 0):
                problems.append(
                    f"{find['id']}: labelled verified but actual is "
                    f"{actual} — the Meter's rules would score that a miss"
                )
            if verdict == "miss" and actual is None:
                problems.append(
                    f"{find['id']}: labelled a miss but nothing was measured — "
                    "a miss is a measured outcome, not an absent one"
                )

    if problems:
        raise SystemExit("seed data is inconsistent:\n  " + "\n  ".join(problems))


def delete_tenant(conn: psycopg.Connection, name: str) -> int:
    """Remove the demo business; every child row cascades."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM business WHERE name = %s", (name,))
        return cur.rowcount


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="delete the demo tenant before seeding")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate the seed files and exit")
    parser.add_argument("--as-of", default=None,
                        help="anchor date for backdating (YYYY-MM-DD, default today)")
    parser.add_argument("--owner-username", default="rosa",
                        help="username for the demo owner login (default 'rosa')")
    parser.add_argument("--owner-password", default=None,
                        help="password for the demo owner login; falls back to "
                             "$DEMO_OWNER_PASSWORD, then a printed random one")
    args = parser.parse_args()

    profile = load("business.json")
    corpus = load("observations.json")["observations"]
    history = load("history.json")["finds"]

    validate(corpus, history)
    anchor = date.fromisoformat(args.as_of) if args.as_of else date.today()

    print(f"seed files valid: {len(corpus)} observations, "
          f"{len(profile['facts'])} facts, {len(history)} finds")
    print(f"backdating relative to {anchor.isoformat()}")
    if args.dry_run:
        print("dry run - nothing written")
        return 0

    settings = Settings.load()
    embedder = build_embedder(settings)
    business_name = profile["business"]["name"]

    with psycopg.connect(settings.cockroach_url, autocommit=True) as conn:
        repo = PostgresRepository(conn)

        if args.reset:
            removed = delete_tenant(conn, business_name)
            print(f"reset: removed {removed} existing tenant(s)")
            # The tenant delete cascades to its rows, but the demo login is
            # keyed on username; drop it explicitly so a re-seed does not
            # collide on "username already taken".
            with conn.cursor() as cur:
                cur.execute("DELETE FROM owner_account WHERE username = %s",
                            (args.owner_username,))
        else:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM business WHERE name = %s",
                            (business_name,))
                if cur.fetchone()[0]:
                    raise SystemExit(
                        f"{business_name!r} already exists. Re-run with --reset to "
                        "replace it."
                    )

        business_id = repo.create_business(
            name=business_name,
            category=profile["business"]["category"],
            city=profile["business"].get("city"),
            goal_monthly_cents=profile["business"].get("goal_monthly_cents"),
        )
        print(f"business {business_name!r} -> {business_id}")

        # A real owner login, so the DEPLOYED board can be demonstrated live —
        # signed in as this account, every figure is read from CockroachDB, not
        # the committed snapshot. The password is never stored, only its scrypt
        # hash. Rosa is fictional demo data, so a shared demo credential is fine.
        owner_password = (args.owner_password
                          or os.environ.get("DEMO_OWNER_PASSWORD")
                          or secrets.token_urlsafe(9))
        account_id = repo.create_account(
            business_id,
            username=args.owner_username,
            password_hash=hash_password(owner_password),
            display_name=profile["business"]["name"],
        )
        print(f"owner login {args.owner_username!r} -> account {account_id}")

        # --- profile ----------------------------------------------------
        fact_texts = [f["fact"] for f in profile["facts"]]
        fact_vectors = embedder.embed(fact_texts)
        for fact, vector in zip(profile["facts"], fact_vectors):
            repo.insert_business_fact(business_id, fact=fact["fact"],
                                      source=fact["source"], embedding=vector)
        print(f"  {len(fact_texts)} business facts")

        for rule in profile["rules"]:
            repo.insert_owner_rule(business_id, rule=rule["rule"],
                                   enabled=rule.get("enabled", True),
                                   cap_cents=rule.get("cap_cents"))
        print(f"  {len(profile['rules'])} owner rules")

        # --- observations ----------------------------------------------
        run_id = repo.start_run(business_id, agent="radar",
                                model_id=settings.embedding_model_id)
        slug_to_id: dict[str, str] = {}
        slug_to_vector: dict[str, list[float]] = {}
        new_count = 0

        for start in range(0, len(corpus), BATCH_SIZE):
            batch = corpus[start:start + BATCH_SIZE]
            vectors = embedder.embed([o["content"] for o in batch])
            for observation, vector in zip(batch, vectors):
                observation_id = repo.insert_observation(
                    business_id,
                    content=observation["content"],
                    kind=observation["kind"],
                    embedding=vector,
                    observed_at=at_days_ago(observation["days_ago"], anchor),
                    source_name=observation.get("source_name"),
                    subject=observation.get("subject"),
                    rating=observation.get("rating"),
                    run_id=run_id,
                )
                if observation_id is None:
                    # Duplicate content in the corpus itself — resolve to the
                    # row already stored so evidence references still work.
                    raise SystemExit(
                        f"observation {observation['id']!r} collided on content "
                        "hash with an earlier row; corpus has duplicate text"
                    )
                slug_to_id[observation["id"]] = observation_id
                slug_to_vector[observation["id"]] = vector
                new_count += 1
            print(f"  observations {start + len(batch)}/{len(corpus)}", end="\r")

        repo.finish_run(run_id, status="ok", note=f"seeded {new_count} observations")
        print(f"  {new_count} observations embedded and stored")

        # --- finds, evidence, verdicts ---------------------------------
        #
        # Evidence similarity is computed, not invented. Each find's title and
        # move are embedded as the query the Analyst would have run, then scored
        # against each cited observation's stored vector. That is the same number
        # the live agent records, so the Evidence viewer shows real retrieval
        # distances for seeded history rather than zeroes or fabrications.
        find_queries = [f"{f['title']}. {f['move']}" for f in history]
        query_vectors = embedder.embed(find_queries)

        verified = misses = estimated = pending = 0
        for find, query_vector in zip(history, query_vectors):
            created = at_days_ago(find["created_days_ago"], anchor, hour=2)
            verify_after = (
                anchor
                - timedelta(days=find["created_days_ago"])
                + timedelta(days=find["verify_window_days"])
            )
            find_id = repo.insert_find_with_evidence(
                business_id,
                emoji=find["emoji"],
                title=find["title"],
                rationale=find["rationale"],
                move=find["move"],
                predicted_daily_cents=find["predicted_daily_cents"],
                confidence=find["confidence"],
                verify_after=verify_after,
                status=find["status"],
                created_at=created,
                decided_at=created + timedelta(hours=6),
                run_id=run_id,
                evidence=[
                    EvidenceRef(
                        slug_to_id[slug],
                        similarity=cosine_similarity(
                            query_vector, slug_to_vector[slug]),
                    )
                    for slug in sorted(
                        find["evidence"],
                        key=lambda s: cosine_similarity(
                            query_vector, slug_to_vector[s]),
                        reverse=True,
                    )
                ],
            )

            ledger = find.get("ledger")
            if ledger is None:
                pending += 1
                continue

            repo.insert_ledger_entry(
                business_id,
                find_id=find_id,
                verdict=ledger["verdict"],
                predicted_daily_cents=find["predicted_daily_cents"],
                actual_daily_cents=ledger["actual_daily_cents"],
                period_start=anchor - timedelta(days=find["created_days_ago"]),
                period_end=verify_after,
                method=ledger["method"],
                note=ledger["note"],
                measured_at=datetime.combine(verify_after, time(hour=6),
                                             tzinfo=timezone.utc),
                run_id=run_id,
            )
            verified += ledger["verdict"] == "verified"
            misses += ledger["verdict"] == "miss"
            estimated += ledger["verdict"] == "estimated"

        print(f"  {len(history)} finds: {verified} verified, {misses} miss, "
              f"{estimated} estimated, {pending} unjudged")

        summary = repo.ledger_summary(business_id)
        rate = "n/a" if summary.hit_rate is None else f"{summary.hit_rate:.0%}"
        print(f"\nledger: {summary.verified_count} verified / "
              f"{summary.judged_count} judged = {rate} hit rate, "
              f"${summary.verified_daily_cents / 100:.2f}/day realized")

        due = repo.due_finds(business_id, today=anchor)
        print(f"Meter's inbox: {len(due)} find(s) awaiting a verdict")

    set_key("BRASSTACKS_BUSINESS_ID", business_id)
    print(f"\nBRASSTACKS_BUSINESS_ID written to .env")
    print("\n" + "=" * 52)
    print("  LIVE DEMO LOGIN — sign in on the deployed /login/:")
    print(f"    username: {args.owner_username}")
    print(f"    password: {owner_password}")
    print("=" * 52)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
