"""The Ask agent, and the MCP connector underneath it.

This is the second CockroachDB tool used at runtime, so what it *does* is a
compliance claim and not merely a feature. Four things here are genuinely risky
and each has a test that fails loudly:

* **Both halves of the MCP connector.** Declaring `mcp_servers` without the
  matching `mcp_toolset` is a 400 at request time, and nothing in the type
  system catches it. The failure is invisible until a live call.
* **The beta endpoint.** The connector only exists on `client.beta.messages`.
  The stub here deliberately has no top-level `.messages`, so calling the wrong
  one is an AttributeError in the test rather than a 404 in production.
* **The tool trail.** An answer that cannot show the query behind it is a
  chatbot answer. The trail is the receipt, and the demo puts it on screen.
* **Answers with no tool calls.** That is the model answering from its own
  knowledge of restaurants instead of from the cluster. It must be detectable,
  because it is the failure most likely to embarrass us on video.

Every test runs offline. The Anthropic client is injected.
"""

from __future__ import annotations

import pytest

from brasstacks.agents.ask import ASK_SYSTEM_PROMPT, ask_system_prompt, run_ask
from brasstacks.providers import (
    Answer,
    FakeAsker,
    McpAsker,
    ModelRefusedError,
    ReasoningError,
    ToolCall,
)
from brasstacks.repository import InMemoryRepository

MCP_URL = "https://cockroachlabs.cloud/mcp"
SERVER = "cockroach"


# --------------------------------------------------------------------------
# Stubs standing in for the anthropic beta client
# --------------------------------------------------------------------------

class StubBlock:
    def __init__(self, type, **fields):
        self.type = type
        for key, value in fields.items():
            setattr(self, key, value)


def text_block(text):
    return StubBlock("text", text=text)


def tool_use(id, name, **input):
    return StubBlock("mcp_tool_use", id=id, name=name, server_name=SERVER,
                     input=input)


def tool_result(tool_use_id, *, is_error=False):
    return StubBlock("mcp_tool_result", tool_use_id=tool_use_id,
                     is_error=is_error, content=[])


class StubMessage:
    def __init__(self, content, stop_reason="end_turn", stop_details=None):
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class _StubBetaMessages:
    def __init__(self, message):
        self._message = message
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._message, Exception):
            raise self._message
        return self._message


class StubBetaClient:
    """A client exposing ONLY `.beta.messages.create`.

    The absence of a top-level `.messages` is deliberate and load-bearing: the
    MCP connector lives on the beta endpoint, and reaching for the stable one
    should blow up here rather than in production.
    """

    def __init__(self, message):
        self.beta = type("_Beta", (), {})()
        self.beta.messages = _StubBetaMessages(message)

    @property
    def calls(self):
        return self.beta.messages.calls


def an_asker(message, *, server_name=SERVER):
    """An McpAsker wired to a stub client, plus the client for assertions."""
    client = StubBetaClient(message)
    asker = McpAsker(client=client, model_id="claude-opus-5", mcp_url=MCP_URL,
                     mcp_token="sekret", server_name=server_name)
    return asker, client


# --------------------------------------------------------------------------
# The MCP request shape
# --------------------------------------------------------------------------

class TestMcpRequestShape:
    def test_sends_both_the_server_and_the_matching_toolset(self):
        # Declaring mcp_servers without the toolset that references it is a 400
        # at request time. Nothing else in the stack catches this.
        asker, client = an_asker(StubMessage([text_block("hi")]))
        asker.ask(system="s", question="q")

        sent = client.calls[0]
        assert sent["mcp_servers"] == [{
            "type": "url",
            "name": SERVER,
            "url": MCP_URL,
            "authorization_token": "sekret",
        }]
        assert sent["tools"] == [
            {"type": "mcp_toolset", "mcp_server_name": SERVER}
        ]

    def test_toolset_name_matches_the_declared_server(self):
        # A mismatch here is the same 400, and is easy to introduce by renaming
        # one of the two in isolation.
        asker, client = an_asker(StubMessage([text_block("hi")]),
                                    server_name="crdb")
        asker.ask(system="s", question="q")

        sent = client.calls[0]
        assert sent["mcp_servers"][0]["name"] == "crdb"
        assert sent["tools"][0]["mcp_server_name"] == "crdb"

    def test_carries_the_mcp_client_beta_flag(self):
        asker, client = an_asker(StubMessage([text_block("hi")]))
        asker.ask(system="s", question="q")

        assert "mcp-client-2025-11-20" in client.calls[0]["betas"]

    def test_goes_through_the_beta_endpoint(self):
        # StubBetaClient has no top-level `.messages`; if the implementation
        # reached for it this would raise AttributeError instead of passing.
        asker, client = an_asker(StubMessage([text_block("hi")]))
        asker.ask(system="s", question="q")

        assert len(client.beta.messages.calls) == 1

    def test_passes_system_prompt_and_question_through(self):
        asker, client = an_asker(StubMessage([text_block("hi")]))
        asker.ask(system="you are the Ask agent", question="how did we do?")

        sent = client.calls[0]
        assert sent["system"] == "you are the Ask agent"
        assert sent["messages"] == [
            {"role": "user", "content": "how did we do?"}
        ]


# --------------------------------------------------------------------------
# Reading the response
# --------------------------------------------------------------------------

class TestAnswerExtraction:
    def test_collects_the_tool_trail_in_order(self):
        asker, _ = an_asker(StubMessage([
            tool_use("t1", "list_databases"),
            tool_result("t1"),
            tool_use("t2", "select_query", sql="SELECT count(*) FROM find"),
            tool_result("t2"),
            text_block("You have 17 finds."),
        ]))
        answer = asker.ask(system="s", question="q")

        assert [c.name for c in answer.tool_calls] == [
            "list_databases", "select_query"]
        assert answer.tool_calls[1].input["sql"].startswith("SELECT count(*)")
        assert answer.text == "You have 17 finds."

    def test_a_failed_tool_does_not_lose_the_turn(self):
        # The MCP server can reject a query while the turn still completes —
        # the model recovers and answers. We must record that it happened.
        asker, _ = an_asker(StubMessage([
            tool_use("t1", "select_query", sql="SELECT * FROM nope"),
            tool_result("t1", is_error=True),
            tool_use("t2", "select_query", sql="SELECT * FROM find"),
            tool_result("t2"),
            text_block("Recovered."),
        ]))
        answer = asker.ask(system="s", question="q")

        assert [c.is_error for c in answer.tool_calls] == [True, False]
        assert answer.text == "Recovered."

    def test_joins_text_split_across_blocks(self):
        # Text arrives interleaved with tool blocks; taking only the first
        # block would silently truncate the answer.
        asker, _ = an_asker(StubMessage([
            text_block("Let me check."),
            tool_use("t1", "select_query", sql="SELECT 1"),
            tool_result("t1"),
            text_block("You made $126.50 a day."),
        ]))
        answer = asker.ask(system="s", question="q")

        assert "Let me check." in answer.text
        assert "You made $126.50 a day." in answer.text

    def test_an_answer_with_no_tool_calls_is_visible(self):
        # This is the model answering from its own knowledge of restaurants
        # rather than from the cluster. It must never be silent.
        asker, _ = an_asker(StubMessage([
            text_block("Restaurants usually make more on Saturdays."),
        ]))
        answer = asker.ask(system="s", question="q")

        assert answer.tool_calls == ()
        assert not answer.queried_the_cluster

    def test_an_answer_with_tool_calls_reports_that_it_queried(self):
        asker, _ = an_asker(StubMessage([
            tool_use("t1", "select_query", sql="SELECT 1"),
            tool_result("t1"),
            text_block("$126.50."),
        ]))
        answer = asker.ask(system="s", question="q")

        assert answer.queried_the_cluster

    def test_unpaired_tool_use_is_still_recorded(self):
        # A turn cut short leaves a tool_use with no result. Dropping it would
        # under-report what the agent actually did.
        asker, _ = an_asker(StubMessage([
            tool_use("t1", "select_query", sql="SELECT 1"),
            text_block("..."),
        ]))
        answer = asker.ask(system="s", question="q")

        assert [c.name for c in answer.tool_calls] == ["select_query"]


class TestFailureModes:
    def test_refusal_raises_before_content_is_read(self):
        details = type("D", (), {"category": "cyber"})()
        asker, _ = an_asker(
            StubMessage([], stop_reason="refusal", stop_details=details))

        with pytest.raises(ModelRefusedError, match="cyber"):
            asker.ask(system="s", question="q")

    def test_max_tokens_is_named_rather_than_returning_a_stub_answer(self):
        asker, _ = an_asker(
            StubMessage([text_block("partial")], stop_reason="max_tokens"))

        with pytest.raises(ReasoningError, match="max_tokens"):
            asker.ask(system="s", question="q")

    def test_client_failure_is_wrapped(self):
        asker, _ = an_asker(RuntimeError("connection reset"))

        with pytest.raises(ReasoningError, match="connection reset"):
            asker.ask(system="s", question="q")

    def test_no_text_block_is_an_error_not_an_empty_answer(self):
        asker, _ = an_asker(StubMessage([
            tool_use("t1", "select_query", sql="SELECT 1"),
            tool_result("t1"),
        ]))

        with pytest.raises(ReasoningError):
            asker.ask(system="s", question="q")


# --------------------------------------------------------------------------
# FakeAsker — the reason the suite runs offline
# --------------------------------------------------------------------------

class TestFakeAsker:
    def test_returns_queued_answers_in_order(self):
        fake = FakeAsker([
            Answer(text="first", tool_calls=()),
            Answer(text="second", tool_calls=()),
        ])
        assert fake.ask(system="s", question="a").text == "first"
        assert fake.ask(system="s", question="b").text == "second"
        assert [c["question"] for c in fake.calls] == ["a", "b"]

    def test_exhaustion_is_loud(self):
        # A silent default would let a test pass while the code made more model
        # calls than the test intended.
        fake = FakeAsker([Answer(text="only", tool_calls=())])
        fake.ask(system="s", question="q")
        with pytest.raises(AssertionError, match="exhausted"):
            fake.ask(system="s", question="q")

    def test_queued_exception_is_raised(self):
        fake = FakeAsker([ModelRefusedError("nope")])
        with pytest.raises(ModelRefusedError):
            fake.ask(system="s", question="q")


# --------------------------------------------------------------------------
# The agent — run rows and the persisted trail
# --------------------------------------------------------------------------

def a_business(repo):
    return repo.create_business(name="Rosa's Trattoria", category="restaurant")


class TestRunAsk:
    def test_writes_a_finished_ask_run(self):
        repo = InMemoryRepository()
        business_id = a_business(repo)
        fake = FakeAsker([Answer(
            text="$126.50 a day.",
            tool_calls=(ToolCall(name="select_query",
                                 input={"sql": "SELECT 1"}, is_error=False),),
        )])

        result = run_ask(repo=repo, asker=fake, business_id=business_id,
                         question="how much have I made?")

        runs = repo.recent_runs(business_id, limit=5)
        assert [r.agent for r in runs] == ["ask"]
        assert runs[0].status == "ok"
        assert result.answer.text == "$126.50 a day."

    def test_persists_the_sql_so_the_ui_can_show_the_receipt(self):
        repo = InMemoryRepository()
        business_id = a_business(repo)
        fake = FakeAsker([Answer(
            text="17 finds.",
            tool_calls=(
                ToolCall(name="select_query",
                         input={"sql": "SELECT count(*) FROM find"},
                         is_error=False),
            ),
        )])

        run_ask(repo=repo, asker=fake, business_id=business_id, question="q")

        note = repo.recent_runs(business_id, limit=1)[0].note
        assert "SELECT count(*) FROM find" in note
        assert "select_query" in note

    def test_a_refusal_still_finishes_the_run(self):
        # A run left in 'running' forever is worse than a recorded failure —
        # it makes the audit trail lie about what the agent did.
        repo = InMemoryRepository()
        business_id = a_business(repo)
        fake = FakeAsker([ModelRefusedError("declined")])

        result = run_ask(repo=repo, asker=fake, business_id=business_id,
                         question="q")

        run = repo.recent_runs(business_id, limit=1)[0]
        assert run.status == "failed"
        assert run.finished_at is not None
        assert result.answer is None
        assert "declined" in result.error

    def test_an_untooled_answer_is_recorded_as_such(self):
        repo = InMemoryRepository()
        business_id = a_business(repo)
        fake = FakeAsker([Answer(text="Probably Saturdays.", tool_calls=())])

        result = run_ask(repo=repo, asker=fake, business_id=business_id,
                         question="q")

        assert not result.answer.queried_the_cluster
        assert "no tool calls" in repo.recent_runs(business_id, limit=1)[0].note

    def test_the_system_prompt_forbids_answering_from_model_knowledge(self):
        # PRODUCT.md voice commitment: it says "I don't know" rather than
        # inventing a number. That has to be in the prompt, not just the docs.
        assert "do not" in ASK_SYSTEM_PROMPT.lower()
        assert "I don't know" in ASK_SYSTEM_PROMPT


class TestAskSystemPrompt:
    """Telling it where the database is, instead of making it search.

    Measured against the live cluster: without this the agent spent six of its
    eight tool calls discovering the cluster id and schema before it could ask
    a real question — on every single question, and one of those six failed
    outright. That is latency and model spend on the demo path.
    """

    CLUSTER = "41b89d47-dce7-419b-8a63-77db2a7044f9"

    def test_names_the_cluster_and_database(self):
        prompt = ask_system_prompt(cluster_id=self.CLUSTER, database="defaultdb")

        assert self.CLUSTER in prompt
        assert "defaultdb" in prompt

    def test_tells_it_not_to_go_looking(self):
        # Naming the cluster is not enough on its own; the model will still
        # call list_clusters unless told the lookup is unnecessary.
        prompt = ask_system_prompt(cluster_id=self.CLUSTER).lower()

        assert "list_clusters" in prompt

    def test_supplies_the_schema_so_it_need_not_introspect(self):
        # Measured: 6 of 10 realistic questions blew through API Gateway's 30s
        # ceiling, with the Lambda still running at 74s. Two of those round
        # trips per question were get_table_schema rediscovering columns that
        # never change.
        prompt = ask_system_prompt(cluster_id=self.CLUSTER)

        assert "get_table_schema" in prompt          # told not to call it
        assert "predicted_daily_cents" in prompt     # ...because it has them
        assert "ledger_entry(" in prompt
        assert "verified|estimated|miss" in prompt

    def test_warns_about_the_time_budget(self):
        # The ceiling is a hard external constraint, not a preference, so the
        # model is told the number rather than just "be quick".
        assert "30 second" in ASK_SYSTEM_PROMPT

    def test_keeps_the_honesty_rules(self):
        # The cluster hint must not displace the constraints that make the
        # answers trustworthy.
        prompt = ask_system_prompt(cluster_id=self.CLUSTER)

        assert "I don't know" in prompt
        assert "INTEGER CENTS" in prompt
        assert "verified" in prompt

    def test_falls_back_cleanly_when_the_cluster_is_unknown(self):
        # Without a configured id it must still work — just slower, by
        # discovering the cluster itself.
        assert ask_system_prompt(cluster_id=None) == ASK_SYSTEM_PROMPT
