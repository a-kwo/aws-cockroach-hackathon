"""Lambda entrypoints, and the secret loading they depend on.

The handlers themselves are thin: they build providers and call the same agent
functions the local harness calls. What is *not* thin, and what is tested here,
is the boundary either side of them —

* **API Gateway event parsing.** A malformed body must become a 400 the caller
  can act on, not a 500 that reads as our fault. The event shape is fiddly and
  is a classic source of silent breakage.
* **SSM hydration.** Lambda has no `.env`. Getting this wrong means a nightly
  run dies at 2 AM on a missing key, which is the exact failure `config.py` was
  written to make loud.

Both run offline; boto3 is injected.
"""

from __future__ import annotations

import json

import pytest

from brasstacks.handlers.ask import parse_question, respond
from brasstacks.handlers.night import summarise
from brasstacks.secrets import hydrate_environment


# --------------------------------------------------------------------------
# Reading the request
# --------------------------------------------------------------------------

def api_event(body, *, raw=False):
    return {"body": body if raw else json.dumps(body)}


class TestParseQuestion:
    def test_reads_the_question_from_a_json_body(self):
        assert parse_question(api_event({"question": "how much?"})) == "how much?"

    def test_trims_surrounding_whitespace(self):
        assert parse_question(api_event({"question": "  how much?  "})) == "how much?"

    def test_a_missing_body_is_a_client_error(self):
        with pytest.raises(ValueError, match="question"):
            parse_question({})

    def test_malformed_json_is_a_client_error(self):
        # A 500 here would read as our fault and hide a caller bug.
        with pytest.raises(ValueError, match="JSON"):
            parse_question(api_event("{not json", raw=True))

    def test_a_blank_question_is_rejected(self):
        with pytest.raises(ValueError, match="question"):
            parse_question(api_event({"question": "   "}))

    def test_an_overlong_question_is_rejected(self):
        # The endpoint proxies a paid model. An unbounded prompt is a bill.
        with pytest.raises(ValueError, match="too long"):
            parse_question(api_event({"question": "x" * 5000}))

    def test_handles_a_body_that_is_already_a_dict(self):
        # Some API Gateway configurations hand the body over pre-parsed.
        assert parse_question({"body": {"question": "hi"}}) == "hi"


class TestRespond:
    def test_shapes_a_json_response(self):
        out = respond(200, {"answer": "yes"})

        assert out["statusCode"] == 200
        assert json.loads(out["body"]) == {"answer": "yes"}
        assert out["headers"]["Content-Type"] == "application/json"

    def test_allows_the_static_site_to_call_it(self):
        # The board is served as static HTML from a different origin, so
        # without CORS the Ask box fails in the browser and works in curl —
        # the most confusing possible way for this to break.
        assert respond(200, {})["headers"]["Access-Control-Allow-Origin"] == "*"


# --------------------------------------------------------------------------
# Reporting the night
# --------------------------------------------------------------------------

class TestSummarise:
    def test_reports_each_agent(self):
        class R:
            note = "12 observed, 3 new"

        class A:
            find_id = "f1"
            retrieved = 24
            error = None

        class M:
            note = "1 judged; 1 verified"

        class K:
            artifact_id = "a1"
            note = "draft saved to s3://b/k"

        out = summarise(type("N", (), {
            "radar": R(), "analyst": A(), "meter": M(), "maker": K()})())

        assert out["radar"] == "12 observed, 3 new"
        assert out["analyst"]["find_id"] == "f1"
        assert out["meter"] == "1 judged; 1 verified"
        assert out["maker"] == "draft saved to s3://b/k"

    def test_a_failed_analyst_is_reported_not_swallowed(self):
        class A:
            find_id = None
            retrieved = 24
            error = "ModelRefusedError: declined"

        out = summarise(type("N", (), {
            "radar": type("R", (), {"note": ""})(),
            "analyst": A(),
            "meter": type("M", (), {"note": ""})(),
            "maker": None,
        })())

        assert out["analyst"]["error"] == "ModelRefusedError: declined"
        assert out["maker"] is None


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------

class StubSsm:
    def __init__(self, params, fail=False):
        self._params = params
        self.fail = fail
        self.calls = []

    def get_parameters_by_path(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("access denied")
        return {"Parameters": [
            {"Name": name, "Value": value}
            for name, value in self._params.items()
        ]}


class PagedStubSsm:
    """SSM as it actually behaves: GetParametersByPath caps a page at 10.

    A caller that reads `Parameters` once and ignores `NextToken` gets the
    first ten and silently loses the rest.
    """

    PAGE_SIZE = 10

    def __init__(self, params):
        self._items = list(params.items())
        self.calls = []

    def get_parameters_by_path(self, **kwargs):
        self.calls.append(kwargs)
        start = int(kwargs.get("NextToken") or 0)
        window = self._items[start:start + self.PAGE_SIZE]
        response = {"Parameters": [
            {"Name": name, "Value": value} for name, value in window
        ]}
        if start + self.PAGE_SIZE < len(self._items):
            response["NextToken"] = str(start + self.PAGE_SIZE)
        return response


class TestHydrateEnvironment:
    def test_reads_every_page_not_just_the_first_ten(self):
        # SSM returns at most 10 parameters per call. Stopping at the first
        # page loses the 11th onward with no error at all: the Lambda boots
        # with a variable missing and fails somewhere unrelated. /brasstacks
        # already holds 9.
        client = PagedStubSsm({
            f"/brasstacks/VAR_{i:02d}": f"value-{i:02d}" for i in range(23)
        })
        env = {}

        loaded = hydrate_environment(env=env, client=client, prefix="/brasstacks")

        assert loaded == 23
        assert env["VAR_00"] == "value-00"
        assert env["VAR_10"] == "value-10", "the 11th parameter was dropped"
        assert env["VAR_22"] == "value-22", "the last page was never requested"
        assert len(client.calls) == 3

    def test_every_page_is_decrypted(self):
        # A second page fetched without WithDecryption would hand back
        # ciphertext, which fails far from here.
        client = PagedStubSsm({f"/brasstacks/V{i}": str(i) for i in range(15)})

        hydrate_environment(env={}, client=client, prefix="/brasstacks")

        assert [c["WithDecryption"] for c in client.calls] == [True, True]

    def test_maps_parameter_names_to_environment_variables(self):
        env = {}
        client = StubSsm({
            "/brasstacks/COCKROACH_DATABASE_URL": "postgresql://x",
            "/brasstacks/ANTHROPIC_API_KEY": "sk-ant-x",
        })

        loaded = hydrate_environment(env=env, client=client, prefix="/brasstacks")

        assert loaded == 2
        assert env["COCKROACH_DATABASE_URL"] == "postgresql://x"
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-x"

    def test_decrypts_secure_strings(self):
        client = StubSsm({"/brasstacks/X": "y"})
        hydrate_environment(env={}, client=client, prefix="/brasstacks")

        assert client.calls[0]["WithDecryption"] is True

    def test_the_real_environment_wins(self):
        # Matches Settings.load(): an explicitly set variable must never be
        # silently overridden by stale remote state.
        env = {"ANTHROPIC_API_KEY": "already-set"}
        client = StubSsm({"/brasstacks/ANTHROPIC_API_KEY": "from-ssm"})

        hydrate_environment(env=env, client=client, prefix="/brasstacks")

        assert env["ANTHROPIC_API_KEY"] == "already-set"

    def test_no_prefix_is_a_no_op(self):
        # Local development and the test suite must not touch AWS.
        env = {}
        assert hydrate_environment(env=env, client=None, prefix=None) == 0
        assert env == {}

    def test_failure_names_the_prefix(self):
        with pytest.raises(RuntimeError, match="/brasstacks"):
            hydrate_environment(env={}, client=StubSsm({}, fail=True),
                                prefix="/brasstacks")
