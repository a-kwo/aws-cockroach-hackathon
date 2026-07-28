"""Model and embedding providers.

These are thin I/O wrappers, but three things in them are genuinely risky and
get tested here:

* **Batching.** Titan embeds one text per invoke_model call, so looping and
  preserving order is our code, not the SDK's. A shuffled result set silently
  attaches the wrong vector to the wrong observation.
* **Dimension agreement.** A model returning anything but 1024 floats must fail
  before the value reaches an INSERT, where the error is an opaque pgcode deep
  inside a nightly run.
* **stop_reason.** Claude can stop for a refusal or on max_tokens. Reading
  `content` without checking first yields empty text or truncated JSON, which
  becomes a confusing parse error far from the cause.

The real clients are injected, so every test here runs offline with no
credentials.
"""

import json

import pytest

from brasstacks.providers import (
    AnthropicReasoner,
    BedrockEmbedder,
    EmbeddingError,
    FakeEmbedder,
    FakeReasoner,
    ModelRefusedError,
    ReasoningError,
)

SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------
# Stubs standing in for boto3 / anthropic clients
# --------------------------------------------------------------------------

class StubBedrockRuntime:
    """Mimics boto3 bedrock-runtime: one embedding per invoke_model call."""

    def __init__(self, dimensions=1024, fail_on=None):
        self.dimensions = dimensions
        self.fail_on = fail_on
        self.calls = []
        self.payloads = []
        self.model_ids = []

    def invoke_model(self, *, modelId, body):
        payload = json.loads(body)
        text = payload["inputText"]
        self.calls.append(text)
        self.payloads.append(payload)
        self.model_ids.append(modelId)
        if self.fail_on and self.fail_on in text:
            raise RuntimeError(f"bedrock exploded on {text!r}")
        # Encode the text's length in position 0 so tests can tell vectors apart.
        vector = [float(len(text))] + [0.0] * (self.dimensions - 1)
        return {"body": _Body(json.dumps({"embedding": vector}))}


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class StubBlock:
    def __init__(self, text, type="text"):
        self.text = text
        self.type = type


class StubMessage:
    def __init__(self, content, stop_reason="end_turn", stop_details=None):
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        self.usage = type("U", (), {"input_tokens": 10, "output_tokens": 20})()
        self.model = "claude-opus-5"


class StubAnthropic:
    def __init__(self, message):
        self._message = message
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._message, Exception):
            raise self._message
        return self._message


# --------------------------------------------------------------------------
# Embedders
# --------------------------------------------------------------------------

class TestBedrockEmbedder:
    def test_embeds_each_text_and_preserves_order(self):
        # Titan takes one text per call, so order is our responsibility.
        stub = StubBedrockRuntime()
        embedder = BedrockEmbedder(client=stub, model_id="titan", dimensions=1024)
        vectors = embedder.embed(["a", "bbb", "cc"])

        assert stub.calls == ["a", "bbb", "cc"]
        assert [v[0] for v in vectors] == [1.0, 3.0, 2.0]

    def test_empty_input_makes_no_api_calls(self):
        stub = StubBedrockRuntime()
        embedder = BedrockEmbedder(client=stub, model_id="titan", dimensions=1024)
        assert embedder.embed([]) == []
        assert stub.calls == []

    def test_wrong_dimension_is_rejected(self):
        # The schema declares VECTOR(1024); anything else must not reach an INSERT.
        stub = StubBedrockRuntime(dimensions=512)
        embedder = BedrockEmbedder(client=stub, model_id="titan", dimensions=1024)
        with pytest.raises(EmbeddingError, match="1024"):
            embedder.embed(["hello"])

    def test_blank_text_is_rejected_before_the_api_call(self):
        # Titan errors on empty input; catching it here names the real problem.
        stub = StubBedrockRuntime()
        embedder = BedrockEmbedder(client=stub, model_id="titan", dimensions=1024)
        with pytest.raises(EmbeddingError, match="empty"):
            embedder.embed(["fine", "   "])

    def test_underlying_failure_is_wrapped_with_context(self):
        stub = StubBedrockRuntime(fail_on="poison")
        embedder = BedrockEmbedder(client=stub, model_id="titan", dimensions=1024)
        with pytest.raises(EmbeddingError, match="index 1"):
            embedder.embed(["ok", "poison"])

    def test_requests_the_configured_dimension_from_titan(self):
        # Titan v2 supports 1024/512/256 and we must not rely on its default
        # happening to match VECTOR(1024). Asking explicitly is what guarantees
        # agreement with the schema, so assert the request actually says so.
        stub = StubBedrockRuntime()
        BedrockEmbedder(
            client=stub, model_id="amazon.titan-embed-text-v2:0", dimensions=1024
        ).embed(["x"])
        assert stub.payloads[0] == {"inputText": "x", "dimensions": 1024}
        assert stub.model_ids[0] == "amazon.titan-embed-text-v2:0"


class TestFakeEmbedder:
    def test_is_deterministic(self):
        a = FakeEmbedder().embed(["tiramisu"])
        b = FakeEmbedder().embed(["tiramisu"])
        assert a == b

    def test_different_texts_give_different_vectors(self):
        # Otherwise similarity assertions in downstream tests are meaningless.
        [v1], [v2] = FakeEmbedder().embed(["tiramisu"]), FakeEmbedder().embed(["parking"])
        assert v1 != v2

    def test_matches_the_schema_dimension(self):
        assert len(FakeEmbedder().embed(["x"])[0]) == 1024

    def test_vectors_are_unit_length(self):
        # Cosine distance on the real index behaves best with normalized input;
        # the fake should not have different numeric character.
        [v] = FakeEmbedder().embed(["anything"])
        assert abs(sum(x * x for x in v) - 1.0) < 1e-9

    def test_records_what_it_was_asked_to_embed(self):
        fake = FakeEmbedder()
        fake.embed(["one", "two"])
        assert fake.embedded == ["one", "two"]


# --------------------------------------------------------------------------
# Reasoners
# --------------------------------------------------------------------------

class TestAnthropicReasoner:
    def _reasoner(self, message):
        return AnthropicReasoner(client=StubAnthropic(message), model_id="claude-opus-5")

    def test_returns_parsed_json(self):
        r = self._reasoner(StubMessage([StubBlock('{"title": "Tiramisu"}')]))
        assert r.complete_json(system="s", user="u", schema=SCHEMA) == {"title": "Tiramisu"}

    def test_sends_the_schema_as_structured_output(self):
        stub = StubAnthropic(StubMessage([StubBlock('{"title": "x"}')]))
        AnthropicReasoner(client=stub, model_id="m").complete_json(
            system="s", user="u", schema=SCHEMA)
        sent = stub.calls[0]
        assert sent["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
        assert sent["model"] == "m"
        assert sent["system"] == "s"

    def test_refusal_raises_rather_than_returning_empty(self):
        # content is empty on a pre-output refusal; parsing it would raise a
        # confusing JSON error instead of naming the actual cause.
        r = self._reasoner(StubMessage([], stop_reason="refusal"))
        with pytest.raises(ModelRefusedError):
            r.complete_json(system="s", user="u", schema=SCHEMA)

    def test_truncated_output_names_max_tokens(self):
        r = self._reasoner(StubMessage([StubBlock('{"title": "Tira')], stop_reason="max_tokens"))
        with pytest.raises(ReasoningError, match="max_tokens"):
            r.complete_json(system="s", user="u", schema=SCHEMA)

    def test_unparseable_json_is_reported_with_the_raw_text(self):
        r = self._reasoner(StubMessage([StubBlock("I'd be happy to help!")]))
        with pytest.raises(ReasoningError, match="happy to help"):
            r.complete_json(system="s", user="u", schema=SCHEMA)

    def test_non_object_json_is_rejected(self):
        r = self._reasoner(StubMessage([StubBlock('["a", "b"]')]))
        with pytest.raises(ReasoningError, match="object"):
            r.complete_json(system="s", user="u", schema=SCHEMA)

    def test_no_text_block_is_reported_clearly(self):
        # Thinking is on by default on Opus 5, so content can lead with a
        # non-text block. Selecting the text block matters.
        r = self._reasoner(StubMessage([StubBlock("", type="thinking")]))
        with pytest.raises(ReasoningError, match="no text"):
            r.complete_json(system="s", user="u", schema=SCHEMA)

    def test_skips_leading_thinking_block_to_find_the_text(self):
        r = self._reasoner(StubMessage([
            StubBlock("pondering", type="thinking"),
            StubBlock('{"title": "found"}'),
        ]))
        assert r.complete_json(system="s", user="u", schema=SCHEMA) == {"title": "found"}

    def test_api_exception_is_wrapped(self):
        r = self._reasoner(RuntimeError("connection reset"))
        with pytest.raises(ReasoningError, match="connection reset"):
            r.complete_json(system="s", user="u", schema=SCHEMA)


class TestFakeReasoner:
    def test_returns_queued_responses_in_order(self):
        fake = FakeReasoner([{"title": "first"}, {"title": "second"}])
        assert fake.complete_json(system="s", user="u", schema=SCHEMA)["title"] == "first"
        assert fake.complete_json(system="s", user="u", schema=SCHEMA)["title"] == "second"

    def test_running_out_of_responses_is_an_error(self):
        # A silent default would let a test pass while the code under test made
        # more model calls than intended.
        fake = FakeReasoner([{"title": "only"}])
        fake.complete_json(system="s", user="u", schema=SCHEMA)
        with pytest.raises(AssertionError, match="exhausted"):
            fake.complete_json(system="s", user="u", schema=SCHEMA)

    def test_records_the_prompts_it_received(self):
        fake = FakeReasoner([{"title": "x"}])
        fake.complete_json(system="sys", user="usr", schema=SCHEMA)
        assert fake.calls[0]["system"] == "sys"
        assert fake.calls[0]["user"] == "usr"

    def test_can_be_told_to_raise(self):
        fake = FakeReasoner([ModelRefusedError("policy")])
        with pytest.raises(ModelRefusedError):
            fake.complete_json(system="s", user="u", schema=SCHEMA)
