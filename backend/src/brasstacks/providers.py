"""Embedding and reasoning providers, behind two narrow interfaces.

Why interfaces at all: AWS could not grant this account access to any current
Claude model on Bedrock, so reasoning runs on the Anthropic API while embeddings
run on Bedrock Titan. Keeping both behind a Protocol means that split is a
config value rather than a structural commitment — if Bedrock access lands,
``BedrockReasoner`` swaps in without touching agent code.

The fakes are the reason the whole unit suite runs offline with no credentials.
They are deliberately deterministic: same text in, same vector out, so a test
asserting on retrieval order is reproducible.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

DEFAULT_MAX_TOKENS = 4096

#: Ask answers are prose, not documents. Small enough to keep the endpoint cheap.
DEFAULT_ASK_MAX_TOKENS = 2048

#: The MCP connector is a first-party Anthropic API feature and is NOT available
#: on Bedrock. Being forced off Bedrock for reasoning is what makes the Ask
#: agent — and therefore the second CockroachDB tool — possible at all.
MCP_BETA_FLAG = "mcp-client-2025-11-20"

#: CockroachDB Cloud's managed MCP server. Streamable HTTP; there is no stdio
#: mode, which is why a remote-URL connector is the only option.
DEFAULT_MCP_URL = "https://cockroachlabs.cloud/mcp"
DEFAULT_MCP_SERVER_NAME = "cockroach"


class ProviderError(RuntimeError):
    """A provider call failed in a way the caller cannot recover from."""


class EmbeddingError(ProviderError):
    pass


class ReasoningError(ProviderError):
    pass


class ModelRefusedError(ReasoningError):
    """Safety classifiers declined the request.

    Distinct from other reasoning failures because retrying the same prompt will
    not help — the caller should log it and move on, not back off and retry.
    """


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------

@runtime_checkable
class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per input text, in the same order."""
        ...


@runtime_checkable
class Reasoner(Protocol):
    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: Mapping[str, Any],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict[str, Any]:
        """Return a JSON object conforming to `schema`."""
        ...


@dataclass(frozen=True)
class ToolCall:
    """One tool the model invoked against the cluster, and how it went.

    ``input`` carries the SQL for ``select_query``. This is the receipt: an
    answer that cannot show the query behind it is a chatbot answer.
    """

    name: str
    input: Mapping[str, Any] = field(default_factory=dict)
    is_error: bool = False


@dataclass(frozen=True)
class Answer:
    """Prose plus the trail that produced it."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()

    @property
    def queried_the_cluster(self) -> bool:
        """Whether this answer actually touched the database.

        A False here means the model answered from its own knowledge of
        restaurants rather than from the owner's data. That is the failure mode
        this product exists to refuse, so it is surfaced rather than inferred.
        """
        return bool(self.tool_calls)


@runtime_checkable
class Asker(Protocol):
    """Owner Q&A over the memory layer.

    Deliberately not folded into ``Reasoner``: this returns prose plus a tool
    trail, and bending ``complete_json`` to carry that would make the Analyst's
    contract worse to serve a different agent.
    """

    def ask(
        self,
        *,
        system: str,
        question: str,
        max_tokens: int = DEFAULT_ASK_MAX_TOKENS,
    ) -> Answer:
        ...


# ---------------------------------------------------------------------------
# Embedders
# ---------------------------------------------------------------------------

class BedrockEmbedder:
    """Amazon Titan Text Embeddings via boto3 bedrock-runtime.

    Titan embeds a single text per request, so batching and order preservation
    are this class's job. A shuffled result would silently attach the wrong
    vector to the wrong observation — the kind of bug that produces plausible
    but wrong retrieval forever.
    """

    def __init__(self, *, client: Any, model_id: str, dimensions: int) -> None:
        self._client = client
        self._model_id = model_id
        self._dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        vectors: list[list[float]] = []
        for index, text in enumerate(texts):
            if not text or not text.strip():
                raise EmbeddingError(
                    f"texts[{index}] is empty or whitespace; Titan rejects empty "
                    "input. Filter before embedding."
                )
            try:
                response = self._client.invoke_model(
                    modelId=self._model_id,
                    body=json.dumps({
                        "inputText": text,
                        # Titan v2 supports 1024/512/256. Ask explicitly rather
                        # than relying on a default matching the schema.
                        "dimensions": self._dimensions,
                    }),
                )
                vector = json.loads(response["body"].read())["embedding"]
            except EmbeddingError:
                raise
            except Exception as e:
                raise EmbeddingError(
                    f"embedding failed at index {index} "
                    f"({type(e).__name__}: {e})"
                ) from e

            if len(vector) != self._dimensions:
                raise EmbeddingError(
                    f"model returned {len(vector)} dimensions, expected "
                    f"{self._dimensions}. The schema declares "
                    f"VECTOR({self._dimensions}); this would fail on INSERT."
                )
            vectors.append([float(x) for x in vector])

        return vectors


class FakeEmbedder:
    """Deterministic offline embedder.

    Derives a stable unit vector from a hash of the text. Not semantic — two
    related sentences are no closer than unrelated ones — so tests that care
    about similarity should compare a text against itself, or stub retrieval
    directly.
    """

    def __init__(self, dimensions: int = 1024) -> None:
        self._dimensions = dimensions
        self.embedded: list[str] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            self.embedded.append(text)
            vectors.append(self._vector(text))
        return vectors

    def _vector(self, text: str) -> list[float]:
        # Stretch the digest to the needed length, then normalize so the vector
        # has the same numeric character as a real embedding.
        raw = b""
        counter = 0
        while len(raw) < self._dimensions * 4:
            raw += hashlib.sha256(f"{counter}:{text}".encode()).digest()
            counter += 1
        floats = [
            struct.unpack("<i", raw[i * 4:(i + 1) * 4])[0] / 2**31
            for i in range(self._dimensions)
        ]
        norm = math.sqrt(sum(f * f for f in floats)) or 1.0
        return [f / norm for f in floats]


# ---------------------------------------------------------------------------
# Reasoners
# ---------------------------------------------------------------------------

def _extract_json(text: str, raw_context: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ReasoningError(
            f"model output was not valid JSON ({e}). Raw text: {raw_context!r}"
        ) from None
    if not isinstance(parsed, dict):
        raise ReasoningError(
            f"model returned a JSON {type(parsed).__name__}, expected an object. "
            f"Raw text: {raw_context!r}"
        )
    return parsed


class AnthropicReasoner:
    """Claude via the first-party Anthropic API, with structured outputs.

    `stop_reason` is checked before `content` is read. A refusal returns an empty
    content list and a max_tokens stop returns truncated JSON; parsing either
    without checking produces an error that points at JSON syntax rather than at
    the real cause.
    """

    def __init__(
        self,
        *,
        client: Any,
        model_id: str,
        effort: str | None = None,
    ) -> None:
        self._client = client
        self._model_id = model_id
        self._effort = effort

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: Mapping[str, Any],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict[str, Any]:
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": dict(schema)}
        }
        if self._effort is not None:
            output_config["effort"] = self._effort

        try:
            message = self._client.messages.create(
                model=self._model_id,
                max_tokens=max_tokens,
                system=system,
                output_config=output_config,
                messages=[{"role": "user", "content": user}],
            )
        except ProviderError:
            raise
        except Exception as e:
            raise ReasoningError(f"{type(e).__name__}: {e}") from e

        stop_reason = getattr(message, "stop_reason", None)

        if stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ModelRefusedError(
                "the model declined this request"
                + (f" (category: {category})" if category else "")
            )

        text = next(
            (b.text for b in message.content
             if getattr(b, "type", None) == "text" and getattr(b, "text", "")),
            None,
        )

        if stop_reason == "max_tokens":
            raise ReasoningError(
                f"output hit max_tokens ({max_tokens}) and is truncated. "
                "Raise max_tokens or narrow the schema."
            )

        if text is None:
            raise ReasoningError(
                "response contained no text block "
                f"(stop_reason={stop_reason!r}, blocks="
                f"{[getattr(b, 'type', '?') for b in message.content]})"
            )

        return _extract_json(text, text[:200])


class BedrockReasoner(AnthropicReasoner):
    """Claude hosted on Bedrock, via the Anthropic SDK's Mantle client.

    Unused today: AWS cannot grant this account access to any current Claude
    model (see CLAUDE.md "Model access"). It exists so that a future grant is a
    config change. The request surface is identical to the first-party API, so
    only construction differs — hence the subclass.

    Note Bedrock model ids carry an `anthropic.` prefix and are not the bare
    first-party ids.
    """


class FakeReasoner:
    """Scripted offline reasoner.

    Responses are consumed in order. Running out raises rather than returning a
    default, because a silent default would let a test pass while the code made
    more model calls than the test intended. Queue an exception instance to
    simulate a failure.
    """

    def __init__(self, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: Mapping[str, Any],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict[str, Any]:
        self.calls.append({
            "system": system,
            "user": user,
            "schema": schema,
            "max_tokens": max_tokens,
        })
        assert self._responses, (
            f"FakeReasoner exhausted after {len(self.calls)} call(s) — the code "
            "under test made more model calls than the test queued responses for"
        )
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return dict(response)


# ---------------------------------------------------------------------------
# Askers — owner Q&A over the CockroachDB MCP server
# ---------------------------------------------------------------------------

def _tool_trail(blocks: Sequence[Any]) -> tuple[ToolCall, ...]:
    """Pair `mcp_tool_use` blocks with their results, preserving call order.

    A tool can fail while the turn still succeeds — the model sees the error and
    recovers. Recording only the successes would overstate what the agent
    actually managed to do, so failures stay in the trail.
    """
    errors: dict[Any, bool] = {
        getattr(b, "tool_use_id", None): bool(getattr(b, "is_error", False))
        for b in blocks
        if getattr(b, "type", None) == "mcp_tool_result"
    }

    calls = []
    for block in blocks:
        if getattr(block, "type", None) != "mcp_tool_use":
            continue
        calls.append(ToolCall(
            name=getattr(block, "name", "unknown"),
            input=dict(getattr(block, "input", None) or {}),
            # An unpaired tool_use means the turn was cut short before the
            # result arrived. Kept in the trail rather than dropped.
            is_error=errors.get(getattr(block, "id", None), False),
        ))
    return tuple(calls)


class McpAsker:
    """Claude answering owner questions over CockroachDB's managed MCP server.

    The tool calls execute **server-side, between Anthropic and CockroachDB
    Cloud** — this process never opens a database connection on this path and
    never runs the SQL itself. That is the point: it is what makes "the agent
    queried the live cluster over the Cloud Managed MCP Server" a true sentence
    rather than a generous reading of one.

    Two request-shape rules that are invisible until a live call:

    * ``mcp_servers`` and a matching ``mcp_toolset`` entry in ``tools`` are both
      required. Declaring the server alone is a 400.
    * The connector only exists on the beta endpoint.

    The server is read-only by default and we never grant write consent, so an
    agent that can answer questions cannot edit the ledger it answers about.
    """

    def __init__(
        self,
        *,
        client: Any,
        model_id: str,
        mcp_url: str = DEFAULT_MCP_URL,
        mcp_token: str,
        server_name: str = DEFAULT_MCP_SERVER_NAME,
    ) -> None:
        self._client = client
        self._model_id = model_id
        self._mcp_url = mcp_url
        self._mcp_token = mcp_token
        self._server_name = server_name

    def ask(
        self,
        *,
        system: str,
        question: str,
        max_tokens: int = DEFAULT_ASK_MAX_TOKENS,
    ) -> Answer:
        try:
            message = self._client.beta.messages.create(
                model=self._model_id,
                max_tokens=max_tokens,
                betas=[MCP_BETA_FLAG],
                system=system,
                mcp_servers=[{
                    "type": "url",
                    "name": self._server_name,
                    "url": self._mcp_url,
                    "authorization_token": self._mcp_token,
                }],
                # Must reference the server declared above by name, or the
                # request is rejected as a validation error.
                tools=[{
                    "type": "mcp_toolset",
                    "mcp_server_name": self._server_name,
                }],
                messages=[{"role": "user", "content": question}],
            )
        except ProviderError:
            raise
        except Exception as e:
            raise ReasoningError(f"{type(e).__name__}: {e}") from e

        stop_reason = getattr(message, "stop_reason", None)

        if stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ModelRefusedError(
                "the model declined this request"
                + (f" (category: {category})" if category else "")
            )

        blocks = list(getattr(message, "content", None) or [])
        tool_calls = _tool_trail(blocks)

        if stop_reason == "max_tokens":
            raise ReasoningError(
                f"answer hit max_tokens ({max_tokens}) and is truncated. "
                "Raise max_tokens or ask a narrower question."
            )

        parts = [
            b.text for b in blocks
            if getattr(b, "type", None) == "text" and getattr(b, "text", "")
        ]
        if not parts:
            raise ReasoningError(
                "response contained no text block "
                f"(stop_reason={stop_reason!r}, "
                f"{len(tool_calls)} tool call(s), blocks="
                f"{[getattr(b, 'type', '?') for b in blocks]})"
            )

        return Answer(text="\n\n".join(parts), tool_calls=tool_calls)


class FakeAsker:
    """Scripted offline Asker.

    Same contract as ``FakeReasoner``: responses are consumed in order and
    running out raises, because a silent default would let a test pass while the
    code made more model calls than the test intended.
    """

    def __init__(self, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def ask(
        self,
        *,
        system: str,
        question: str,
        max_tokens: int = DEFAULT_ASK_MAX_TOKENS,
    ) -> Answer:
        self.calls.append({
            "system": system,
            "question": question,
            "max_tokens": max_tokens,
        })
        assert self._responses, (
            f"FakeAsker exhausted after {len(self.calls)} call(s) — the code "
            "under test made more model calls than the test queued answers for"
        )
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


# ---------------------------------------------------------------------------
# Construction from settings
# ---------------------------------------------------------------------------

def build_embedder(settings: Any) -> Embedder:
    """Real Bedrock embedder from settings. Import boto3 lazily so the unit
    suite never pays for it."""
    import boto3

    return BedrockEmbedder(
        client=boto3.client("bedrock-runtime", region_name=settings.aws_region),
        model_id=settings.embedding_model_id,
        dimensions=settings.embedding_dimensions,
    )


def build_reasoner(settings: Any, *, effort: str | None = None) -> Reasoner:
    """Real reasoner for whichever provider is configured."""
    if settings.model_provider == "bedrock":
        from anthropic import AnthropicBedrockMantle

        return BedrockReasoner(
            client=AnthropicBedrockMantle(aws_region=settings.aws_region),
            model_id=settings.reasoning_model_id,
            effort=effort,
        )

    import anthropic

    return AnthropicReasoner(
        client=anthropic.Anthropic(api_key=settings.anthropic_api_key),
        model_id=settings.reasoning_model_id,
        effort=effort,
    )


def build_asker(settings: Any) -> Asker:
    """Real Asker from settings.

    Always the first-party Anthropic client, even when ``MODEL_PROVIDER=bedrock``:
    the MCP connector does not exist on Bedrock, so there is no Bedrock variant
    of this to fall back to. Fails naming the variable rather than sending an
    unauthenticated request that returns a confusing downstream error.
    """
    from brasstacks.config import ConfigError

    if not getattr(settings, "cockroach_mcp_token", None):
        raise ConfigError(
            "COCKROACH_MCP_TOKEN is required for the Ask agent. Create a "
            "service account in the CockroachDB Cloud Console, scope its RBAC "
            "to this cluster, and copy the key from the generated MCP config."
        )
    if not settings.anthropic_api_key:
        raise ConfigError(
            "ANTHROPIC_API_KEY is required for the Ask agent — the MCP "
            "connector is a first-party API feature and is unavailable on "
            "Bedrock, so this path cannot use MODEL_PROVIDER=bedrock."
        )

    import anthropic

    return McpAsker(
        client=anthropic.Anthropic(api_key=settings.anthropic_api_key),
        model_id=settings.anthropic_model_id,
        mcp_url=settings.cockroach_mcp_url,
        mcp_token=settings.cockroach_mcp_token,
    )
