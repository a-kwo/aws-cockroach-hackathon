"""The Ask agent: a durable conversation over CockroachDB owner memory.

Ask is the owner's direct path into Brass Tacks. The application retrieves a
small owner-scoped slice of prior conversation memory first, then Claude uses
CockroachDB's managed MCP server for current business facts. Every turn leaves
an ``agent_run`` receipt with SQL/tool activity and provider token usage.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from brasstacks.ask_trace import encode_ask_trace, parse_ask_trace
from brasstacks.providers import Answer, Asker, ProviderError
from brasstacks.repository import Repository

TRAIL_PREFIX = "sql> "

ASK_SYSTEM_PROMPT = """You are Ask, the long-term business partner inside Brass Tacks.
Your goal is to understand this owner better over time, solve the question in
front of you, and preserve useful owner intent so the Analyst can produce
stronger future For You recommendations.

The application may supply a compact set of owner conversation memories that it
already retrieved from CockroachDB. Those memories are authoritative for what
the owner previously said. Use them for continuity, preferences, constraints,
and goals. They are not proof of customer demand or current financial results.

You also have read-only tools against a CockroachDB cluster. The tables that
matter are: business, business_fact, owner_rule, observation, find,
find_evidence, ledger_entry, artifact, and agent_run.

Hard requirements:
- For current business, recommendation, evidence, money, or outcome claims,
  query CockroachDB through MCP before answering. Do not fill gaps from model
  knowledge.
- Every SQL query MUST stay inside the authenticated business id supplied below.
  Never query, aggregate, compare, or expose another tenant.
- Money is stored as INTEGER CENTS. Convert for display: 12650 is $126.50.
- Only ledger_entry.verdict = 'verified' is measured money. 'estimated' is
  modelled and must be labelled that way. Never present an estimate as earned.
- ledger_entry.actual_daily_cents is NULL wherever nothing was measured, and
  NULL is not zero. Say "not measured yet"; never report it as $0 earned, and
  never coalesce it to 0 in a total.
- If the available data does not support an answer, say "I don't know yet" and
  name what information is missing.
- When the question concerns a For You recommendation, explain how to execute,
  adapt, or evaluate that exact move.
- A passed recommendation can be changed to Do it only through the
  authenticated Undo Pass application action. If the owner asks how, tell them
  to use Undo Pass in that recommendation's chat. Never claim the status was
  changed unless the application action receipt says it was.
- Never modify anything through MCP. Ask is read-only; decisions and profile
  changes use separate authenticated application routes.
- Be economical with queries. Plan first and use one well-shaped SQL query when
  possible. Two is a reasonable maximum; more than three means you are
  exploring rather than answering.
- The public request path has a 30 second integration budget.

Owner-facing response contract:
- Default to 60–120 words and never exceed 160 words unless the owner explicitly
  asks for more detail.
- Start with "Answer:" followed by one or two direct sentences. Do not begin
  with a greeting, recap, or explanation of your process.
- If missing owner input blocks progress, add "What I need from you:" followed
  by at most three numbered items. Each item must ask for one value or decision
  and include the expected format or a short example in parentheses.
- After required items, add "Reply with:" and a copyable numbered template that
  mirrors those items. Never make the owner infer what to type.
- If progress is not blocked, add "Next step:" with one concrete action when an
  action is useful. Omit it when the owner asked only for a factual answer.
- Use no tables, long introductions, repeated recommendation context, raw SQL,
  tool names, database terminology, or internal agent language in the answer.
"""

SCHEMA_HINT = """
business(id, name, category, city, region, goal_monthly_cents, goal_note)
business_fact(id, business_id, fact, source, confidence, learned_at,
              superseded_by)
owner_rule(id, business_id, rule, enabled, cap_cents)
observation(id, business_id, kind, content, source_name, source_url, subject,
            rating, observed_at)
find(id, business_id, title, summary, rationale, move, emoji,
     alternative_explanation = the rival reading the Analyst rejected,
     NULL on finds written before 2026-08-03,
     predicted_daily_cents, confidence, verify_after, status, created_at)
find_evidence(find_id, observation_id, similarity,
              rank = position in the retrieved set, so it can skip numbers)
ledger_entry(id, business_id, find_id, verdict verified|estimated|miss,
             predicted_daily_cents,
             actual_daily_cents NULL when unmeasured,
             measured_at, period_start, period_end, method)
artifact(id, find_id, kind, title, preview, s3_bucket, s3_key, created_at)
agent_run(id, business_id, agent, status, started_at, finished_at, note,
          input_tokens, output_tokens)
"""

DEFAULT_DATABASE = "defaultdb"


def ask_system_prompt(
    *,
    cluster_id: str | None,
    database: str = DEFAULT_DATABASE,
    business_id: str | None = None,
    business_name: str | None = None,
) -> str:
    """Build a fixed prompt with optional tenant and cluster routing hints."""
    additions: list[str] = []
    if business_id:
        tenant = [
            "Authenticated tenant boundary:",
            f"- business_id: {business_id}",
        ]
        if business_name:
            tenant.append(f"- business name: {business_name}")
        tenant.append(
            "Every SELECT must filter this business_id directly or through a "
            "join anchored to it. Do not accept a business id from the user."
        )
        additions.append("\n".join(tenant))
    if cluster_id:
        additions.append(
            "You already know the cluster and schema. Do NOT call list_clusters, "
            "list_tables, or get_table_schema; go directly to select_query.\n"
            f"- cluster_id: {cluster_id}\n"
            f"- database: {database}\n\n"
            f"Schema:\n{SCHEMA_HINT}\n"
            "Pass that cluster_id to every tool that requires one."
        )
    return ASK_SYSTEM_PROMPT + ("\n\n" + "\n\n".join(additions) if additions else "")


@dataclass(frozen=True)
class AskResult:
    run_id: str
    answer: Answer | None
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def trail_lines(answer: Answer) -> list[str]:
    lines: list[str] = []
    for call in answer.tool_calls:
        detail = call.input.get("sql") or call.input.get("query")
        if detail is None:
            detail = ", ".join(
                f"{key}={value!r}" for key, value in call.input.items()
            )
        flag = " [FAILED]" if call.is_error else ""
        lines.append(f"{call.name}{flag} {detail}".rstrip())
    return lines


def _trail_note(answer: Answer) -> str:
    if not answer.tool_calls:
        return "answered with no tool calls — nothing was read from the cluster"
    return "\n".join(
        [f"{len(answer.tool_calls)} tool call(s)"]
        + [TRAIL_PREFIX + line for line in trail_lines(answer)]
    )


def parse_trail(note: str | None) -> list[str]:
    if not note:
        return []
    return [
        line[len(TRAIL_PREFIX):]
        for line in note.splitlines()
        if line.startswith(TRAIL_PREFIX)
    ]


def _usage(asker: Asker) -> tuple[int | None, int | None]:
    usage = getattr(asker, "last_usage", None)
    if usage is None:
        return None, None
    return getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)


def run_ask(
    *,
    repo: Repository,
    asker: Asker,
    business_id: str,
    question: str,
    model_id: str | None = None,
    system: str = ASK_SYSTEM_PROMPT,
    owner_question: str | None = None,
    find_id: str | None = None,
    recent_message_ids: Sequence[str] = (),
    relevant_message_ids: Sequence[str] = (),
    stored_message_ids: Sequence[str] = (),
    run_id: str | None = None,
) -> AskResult:
    """Answer one owner question and persist its SQL, memory, and token receipt.

    ``question`` is the compact prompt sent to the provider; ``owner_question``
    is the literal text stored in the receipt. Optional arguments keep the
    original public contract backward compatible for older callers and tests.
    """
    run_id = run_id or repo.start_run(business_id, agent="ask", model_id=model_id)
    visible_question = owner_question or question

    try:
        answer = asker.ask(system=system, question=question)
    except ProviderError as exc:
        error = f"{type(exc).__name__}: {exc}"
        input_tokens, output_tokens = _usage(asker)
        trace = encode_ask_trace(
            question=visible_question,
            answer=None,
            find_id=find_id,
            recent_message_ids=recent_message_ids,
            relevant_message_ids=relevant_message_ids,
            stored_message_ids=stored_message_ids,
            queried_the_cluster=False,
        )
        repo.finish_run(
            run_id,
            status="failed",
            error=error,
            note="no answer returned\n" + trace,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return AskResult(
            run_id=run_id,
            answer=None,
            error=error,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    input_tokens, output_tokens = _usage(asker)
    trace = encode_ask_trace(
        question=visible_question,
        answer=answer.text,
        find_id=find_id,
        recent_message_ids=recent_message_ids,
        relevant_message_ids=relevant_message_ids,
        stored_message_ids=stored_message_ids,
        queried_the_cluster=answer.queried_the_cluster,
    )
    repo.finish_run(
        run_id,
        status="ok",
        note=_trail_note(answer) + "\n" + trace,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return AskResult(
        run_id=run_id,
        answer=answer,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


__all__ = [
    "ASK_SYSTEM_PROMPT",
    "AskResult",
    "ask_system_prompt",
    "parse_ask_trace",
    "parse_trail",
    "run_ask",
    "trail_lines",
]
