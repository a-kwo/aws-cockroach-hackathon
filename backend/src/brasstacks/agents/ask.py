"""The Ask agent — the owner reaching into the memory layer directly.

Every other agent decides what the owner sees. This one lets her ask. She types
a question and the agent answers it by querying the live cluster read-only over
CockroachDB Cloud's managed MCP server.

This is the second CockroachDB tool used at runtime, which makes it a compliance
claim rather than a convenience: the hackathon rules require at least two tools
meaningfully used, and the judging question is literally *"what did the agent
actually do with the tool?"* The answer here is: it ran SQL against the live
cluster, and the SQL is on the record.

Three honesty constraints, each a claim about the owner's money or her data:

* **Read-only, enforced twice.** The MCP server defaults to read-only and we
  never grant write consent; the service account's Cloud RBAC is scoped to this
  cluster. An agent that answers questions must not be able to edit the ledger
  it answers about.
* **The trail is the receipt.** Same principle as ``find_evidence``. Every turn
  writes an ``agent_run`` row carrying the executed SQL, and the demo puts it on
  screen.
* **It says "I don't know."** The prompt forbids answering from the model's own
  knowledge of restaurants. An answer that touched no tools is recorded as such
  rather than passed off as fact.
"""

from __future__ import annotations

from dataclasses import dataclass

from brasstacks.providers import Answer, Asker, ProviderError, ToolCall
from brasstacks.repository import Repository

#: Prefix on every trail line, so the UI can split the note deterministically
#: rather than guessing at its shape.
TRAIL_PREFIX = "sql> "

ASK_SYSTEM_PROMPT = """You are the Ask agent for Brass Tacks. You answer an \
owner's questions about her own business by querying her database directly.

You have read-only tools against a CockroachDB cluster. The tables that matter \
are: business, observation (what we have seen), find (moves we proposed, with \
predicted_daily_cents and verify_after), find_evidence (which observations \
drove each find, with cosine similarity), ledger_entry (verdicts: verified, \
estimated, miss), and agent_run (the audit trail).

Hard requirements:
- ALWAYS query before answering. Do not answer from your own knowledge of \
restaurants or of typical businesses. If you did not read it out of her \
database, you do not know it.
- Money is stored as INTEGER CENTS. Convert for display: 12650 is $126.50.
- Only a ledger_entry with verdict = 'verified' is money that was actually \
measured. An 'estimated' row is modelled and must be described that way. Never \
present an estimate as earned.
- If a query returns nothing, say "I don't know" or say that there is no data \
for it. Do not fill the gap with a plausible number. An invented figure about \
her revenue is the worst thing you can produce.
- Answer in plain language, name amounts and dates, and keep it SHORT — a few \
sentences, or a short list. She is reading this between other jobs, standing up. \
Even for a broad question, give her the two or three things that matter, not an \
essay. Long answers are also slow, and the request is cut off after 30 seconds.
- Never modify anything. You have read access only, and that is deliberate.
- Be economical with queries. Every tool call is a slow round trip, and the \
request is cut off after 30 seconds. Plan the whole answer first, then write ONE \
query that gets everything you need — joins and aggregates in a single \
statement, not a series of exploratory ones. Two queries is a reasonable \
maximum; more than three means you are exploring rather than answering."""


#: Column lists for the tables the Ask agent actually queries. Supplied up front
#: because otherwise the model spends two or more `get_table_schema` round trips
#: rediscovering them on every question — measured at ~5s each against a 30s
#: ceiling. Kept deliberately partial: these are the columns worth querying, not
#: a mirror of db/schema.sql, and the agent can still introspect anything else.
SCHEMA_HINT = """
business(id, name, category, city, goal_monthly_cents)
observation(id, business_id, kind, content, source_name, subject, rating,
            observed_at)   -- kind: review|rival_price|rival_menu|trend|social
find(id, business_id, title, rationale, move, emoji, predicted_daily_cents,
     confidence, verify_after, status, created_at)
     -- status: proposed|accepted|later|rejected|live|retired
find_evidence(find_id, observation_id, similarity, rank)
ledger_entry(id, business_id, find_id, verdict, predicted_daily_cents,
             actual_daily_cents, measured_at, period_start, period_end, method)
             -- verdict: verified|estimated|miss
artifact(id, find_id, kind, title, preview, s3_bucket, s3_key, created_at)
agent_run(id, business_id, agent, status, started_at, finished_at, note)
"""


DEFAULT_DATABASE = "defaultdb"


def ask_system_prompt(*, cluster_id: str | None,
                      database: str = DEFAULT_DATABASE) -> str:
    """The prompt, with the cluster named so the agent can skip discovery.

    Measured against the live cluster: without this, six of eight tool calls
    went on finding the cluster id and reading schemas before the first real
    query — repeated on every question, with one call failing outright because
    the query tools require a cluster id the agent had not yet fetched.

    Returns the plain prompt when no cluster is configured, so an unset value
    costs latency rather than correctness.
    """
    if not cluster_id:
        return ASK_SYSTEM_PROMPT

    return ASK_SYSTEM_PROMPT + f"""

You already know where the data is, and what shape it is. Do NOT call \
list_clusters, list_tables, or get_table_schema — every one is a wasted round \
trip against a 30-second budget.

- cluster_id: {cluster_id}
- database: {database}

Schema (the columns worth querying):
{SCHEMA_HINT}
Pass that cluster_id to every tool that takes one; they fail without it. Go \
straight to select_query."""


@dataclass(frozen=True)
class AskResult:
    run_id: str
    answer: Answer | None
    error: str | None = None


def trail_lines(answer: Answer) -> list[str]:
    """The trail as display lines: what was called, and what it ran.

    Public because the API handler returns this to the browser and the run note
    stores it — both should format the receipt the same way rather than each
    inventing one.
    """
    lines = []
    for call in answer.tool_calls:
        detail = call.input.get("sql") or call.input.get("query")
        if detail is None:
            # Non-query tools (list_databases, get_table_schema) carry their
            # arguments instead of SQL.
            detail = ", ".join(f"{k}={v!r}" for k, v in call.input.items())
        flag = " [FAILED]" if call.is_error else ""
        lines.append(f"{call.name}{flag} {detail}".rstrip())
    return lines


def _trail_note(answer: Answer) -> str:
    """The stored form: a count, then one prefixed line per call.

    Recorded even when empty — an answer with no tool calls is the model
    reasoning from its own knowledge instead of from the cluster, and that is
    exactly the thing this note exists to make visible.
    """
    if not answer.tool_calls:
        return "answered with no tool calls — nothing was read from the cluster"

    return "\n".join(
        [f"{len(answer.tool_calls)} tool call(s)"]
        + [TRAIL_PREFIX + line for line in trail_lines(answer)]
    )


def parse_trail(note: str | None) -> list[str]:
    """Recover the trail lines from a stored note.

    The inverse of `_trail_note`, so the UI and the tests read the note the same
    way rather than each inventing a parser.
    """
    if not note:
        return []
    return [
        line[len(TRAIL_PREFIX):]
        for line in note.splitlines()
        if line.startswith(TRAIL_PREFIX)
    ]


def run_ask(
    *,
    repo: Repository,
    asker: Asker,
    business_id: str,
    question: str,
    model_id: str | None = None,
    system: str = ASK_SYSTEM_PROMPT,
) -> AskResult:
    """Answer one owner question, and leave a record that it happened."""
    run_id = repo.start_run(business_id, agent="ask", model_id=model_id)

    try:
        answer = asker.ask(system=system, question=question)
    except ProviderError as e:
        error = f"{type(e).__name__}: {e}"
        # Finishing the run matters more than it looks: a row left 'running'
        # forever makes the audit trail lie about what the agent did.
        repo.finish_run(run_id, status="failed", error=error,
                        note="no answer returned")
        return AskResult(run_id=run_id, answer=None, error=error)

    repo.finish_run(run_id, status="ok", note=_trail_note(answer))
    return AskResult(run_id=run_id, answer=answer)
