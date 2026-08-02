"""The button that asks for a first night, now.

Every guard here exists because this endpoint spends money: a Tavily search,
roughly fifty Bedrock embeddings and a Claude call per press.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from brasstacks.auth import hash_password, issue_session_token
from brasstacks.handlers.run import start_night
from brasstacks.repository import InMemoryRepository

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
NIGHT_FN = "brasstacks-NightFunction-abc"


class FakeInvoker:
    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return {"StatusCode": 202}


@pytest.fixture
def repo():
    return InMemoryRepository()


def signed_in(repo, *, with_business=True):
    business = repo.create_business(name="Asaka", category="restaurant") \
        if with_business else None
    account = repo.create_account(business, username="sam",
                                  password_hash=hash_password("x"))
    token, fingerprint, expires = issue_session_token(now=NOW)
    repo.create_session(fingerprint, business_id=business,
                        account_id=account, expires_at=expires)
    return token, business


def call(repo, token, invoker):
    event = {"headers": {"Authorization": f"Bearer {token}"}} if token else {}
    return start_night(event, repo=repo, invoker=invoker,
                       night_function=NIGHT_FN, now=NOW)


class TestStartNight:
    def test_it_fires_the_night_for_the_signed_in_business(self, repo):
        token, business = signed_in(repo)
        invoker = FakeInvoker()

        response = call(repo, token, invoker)

        assert response["statusCode"] == 202
        assert json.loads(response["body"])["status"] == "started"
        [sent] = invoker.calls
        assert sent["FunctionName"] == NIGHT_FN
        assert json.loads(sent["Payload"])["business_id"] == business

    def test_it_does_not_wait_for_the_night(self, repo):
        # API Gateway caps an integration at 30s and a night runs 60-90. A
        # synchronous invoke would 504 while the work carried on invisibly.
        token, _ = signed_in(repo)
        invoker = FakeInvoker()

        call(repo, token, invoker)

        assert invoker.calls[0]["InvocationType"] == "Event"

    def test_no_session_spends_nothing(self, repo):
        invoker = FakeInvoker()

        response = call(repo, None, invoker)

        assert response["statusCode"] == 401
        assert invoker.calls == []

    def test_an_account_with_no_business_spends_nothing(self, repo):
        token, _ = signed_in(repo, with_business=False)
        invoker = FakeInvoker()

        response = call(repo, token, invoker)

        assert response["statusCode"] == 409
        assert invoker.calls == []

    def test_the_tenant_is_never_taken_from_the_request(self, repo):
        """Otherwise any signed-in owner could spend model calls against any
        other business in the cluster."""
        token, mine = signed_in(repo)
        someone_else = repo.create_business(name="Theirs", category="restaurant")
        invoker = FakeInvoker()

        event = {"headers": {"Authorization": f"Bearer {token}"},
                 "body": json.dumps({"business_id": someone_else})}
        start_night(event, repo=repo, invoker=invoker,
                    night_function=NIGHT_FN, now=NOW)

        assert json.loads(invoker.calls[0]["Payload"])["business_id"] == mine

    def test_a_second_press_does_not_start_a_second_night(self, repo):
        # A double-click, or an impatient reload, would otherwise pay twice.
        token, business = signed_in(repo)
        repo.start_run(business, agent="radar")
        invoker = FakeInvoker()

        response = call(repo, token, invoker)

        assert response["statusCode"] == 202
        assert json.loads(response["body"])["status"] == "running"
        assert invoker.calls == []

    def test_a_business_that_already_had_a_night_is_not_re_run(self, repo):
        token, business = signed_in(repo)
        run = repo.start_run(business, agent="radar")
        repo.finish_run(run, status="ok")
        invoker = FakeInvoker()

        response = call(repo, token, invoker)

        assert json.loads(response["body"])["status"] == "done"
        assert invoker.calls == []

    def test_an_unconfigured_runner_is_reported_not_guessed(self, repo):
        token, _ = signed_in(repo)
        invoker = FakeInvoker()

        response = start_night(
            {"headers": {"Authorization": f"Bearer {token}"}},
            repo=repo, invoker=invoker, night_function=None, now=NOW)

        assert response["statusCode"] == 503
        assert invoker.calls == []
