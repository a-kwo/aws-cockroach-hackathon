"""Configuration, resolved once at startup.

Two entry points:

* ``Settings.from_env(mapping)`` — pure, takes any mapping. Everything is
  validated here so it can be tested without touching the process environment.
* ``Settings.load()`` — reads ``os.environ``, falling back to a repo-root
  ``.env`` for values the environment does not already define. Deployed Lambdas
  get real environment variables and never see a ``.env``.

The bias is to fail at startup with the offending variable named. A nightly run
that dies at 2 AM on a missing key, or an embedding dimension that silently
disagrees with the schema, costs far more to diagnose than a loud boot failure.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

#: Must match VECTOR(1024) in db/schema.sql. Changing one without the other
#: fails on every insert, so the mismatch is rejected at startup.
SCHEMA_EMBEDDING_DIMENSIONS = 1024

VALID_PROVIDERS = ("anthropic", "bedrock")

#: CockroachDB Cloud's managed MCP endpoint. Kept here rather than imported from
#: providers so that config has no dependency on the provider layer.
DEFAULT_MCP_URL = "https://cockroachlabs.cloud/mcp"

REPO_ROOT = Path(__file__).resolve().parents[3]


class ConfigError(RuntimeError):
    """Configuration is missing or inconsistent. Names the variable at fault."""


def _clean(value: str | None) -> str | None:
    """Treat whitespace-only as absent — .env files are full of empty placeholders."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _mask_url_password(url: str) -> str:
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


@dataclass(frozen=True)
class Settings:
    # --- CockroachDB ---
    cockroach_url: str = field(repr=False)

    # --- AWS ---
    aws_region: str
    embedding_model_id: str
    embedding_dimensions: int
    s3_artifact_bucket: str

    # --- Reasoning ---
    model_provider: str
    anthropic_api_key: str | None = field(repr=False)
    anthropic_model_id: str
    bedrock_model_id: str | None

    # --- Optional ---
    business_id: str | None = None
    search_api_key: str | None = field(default=None, repr=False)

    # --- Ask agent, over the CockroachDB managed MCP server ---
    # Optional at this level on purpose: Radar, Analyst and Meter have no
    # business failing to boot because owner Q&A is unconfigured. build_asker()
    # raises, naming the variable, only when the Ask path is actually used.
    cockroach_mcp_url: str = DEFAULT_MCP_URL
    cockroach_mcp_token: str | None = field(default=None, repr=False)
    # Not a secret — an identifier. Supplying it lets the Ask agent skip the
    # cluster-discovery round trips it would otherwise repeat every question.
    cockroach_cluster_id: str | None = None
    cockroach_database: str = "defaultdb"

    @property
    def reasoning_model_id(self) -> str:
        """The model id for whichever provider is configured.

        Callers should not have to branch on the provider to name a model.
        """
        if self.model_provider == "bedrock":
            assert self.bedrock_model_id is not None  # enforced in from_env
            return self.bedrock_model_id
        return self.anthropic_model_id

    @property
    def safe_cockroach_url(self) -> str:
        """The connection string with the password masked, for logs."""
        return _mask_url_password(self.cockroach_url)

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        # Explicit rather than relying on field(repr=False) alone, so that a
        # future field addition cannot silently start leaking a secret.
        return (
            f"Settings(provider={self.model_provider!r}, "
            f"model={self.reasoning_model_id!r}, "
            f"region={self.aws_region!r}, "
            f"embedding_model={self.embedding_model_id!r}, "
            f"dimensions={self.embedding_dimensions}, "
            f"bucket={self.s3_artifact_bucket!r}, "
            f"cockroach={self.safe_cockroach_url!r}, "
            f"anthropic_api_key={'set' if self.anthropic_api_key else 'unset'}, "
            f"search_api_key={'set' if self.search_api_key else 'unset'}, "
            f"mcp_url={self.cockroach_mcp_url!r}, "
            f"mcp_token={'set' if self.cockroach_mcp_token else 'unset'})"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        def required(name: str) -> str:
            value = _clean(env.get(name))
            if value is None:
                raise ConfigError(
                    f"{name} is required but missing or blank. "
                    "Copy .env.example to .env and fill it in, or set the "
                    "variable in the environment."
                )
            return value

        provider = (_clean(env.get("MODEL_PROVIDER")) or "anthropic").lower()
        if provider not in VALID_PROVIDERS:
            raise ConfigError(
                f"MODEL_PROVIDER must be one of {VALID_PROVIDERS}, got {provider!r}"
            )

        raw_dimensions = _clean(env.get("EMBEDDING_DIMENSIONS"))
        if raw_dimensions is None:
            dimensions = SCHEMA_EMBEDDING_DIMENSIONS
        else:
            try:
                dimensions = int(raw_dimensions)
            except ValueError:
                raise ConfigError(
                    f"EMBEDDING_DIMENSIONS must be an integer, got {raw_dimensions!r}"
                ) from None
        if dimensions != SCHEMA_EMBEDDING_DIMENSIONS:
            raise ConfigError(
                f"EMBEDDING_DIMENSIONS is {dimensions} but db/schema.sql declares "
                f"VECTOR({SCHEMA_EMBEDDING_DIMENSIONS}). Every insert would fail. "
                "Change both or neither."
            )

        anthropic_key = _clean(env.get("ANTHROPIC_API_KEY"))
        bedrock_model = _clean(env.get("BEDROCK_MODEL_ID"))

        if provider == "anthropic" and anthropic_key is None:
            raise ConfigError(
                "ANTHROPIC_API_KEY is required when MODEL_PROVIDER=anthropic. "
                "Get one at https://console.anthropic.com/settings/keys"
            )
        if provider == "bedrock" and bedrock_model is None:
            raise ConfigError(
                "BEDROCK_MODEL_ID is required when MODEL_PROVIDER=bedrock "
                "(e.g. anthropic.claude-opus-5 — note the provider prefix)"
            )

        return cls(
            cockroach_url=required("COCKROACH_DATABASE_URL"),
            aws_region=required("AWS_REGION"),
            embedding_model_id=required("BEDROCK_EMBEDDING_MODEL_ID"),
            embedding_dimensions=dimensions,
            s3_artifact_bucket=required("S3_ARTIFACT_BUCKET"),
            model_provider=provider,
            anthropic_api_key=anthropic_key,
            anthropic_model_id=_clean(env.get("ANTHROPIC_MODEL_ID")) or "claude-opus-5",
            bedrock_model_id=bedrock_model,
            business_id=_clean(env.get("BRASSTACKS_BUSINESS_ID")),
            search_api_key=_clean(env.get("SEARCH_API_KEY")),
            cockroach_mcp_url=(
                _clean(env.get("COCKROACH_MCP_URL")) or DEFAULT_MCP_URL
            ),
            cockroach_mcp_token=_clean(env.get("COCKROACH_MCP_TOKEN")),
            cockroach_cluster_id=_clean(env.get("COCKROACH_CLUSTER_ID")),
            cockroach_database=(
                _clean(env.get("COCKROACH_DATABASE")) or "defaultdb"
            ),
        )

    @classmethod
    def load(cls, env_file: Path | None = None) -> Settings:
        """Resolve settings from the process environment, with .env as fallback.

        The real environment always wins — a deployed Lambda has no .env, and a
        stale local file must never override an explicitly exported value.
        """
        merged = dict(os.environ)
        path = env_file if env_file is not None else REPO_ROOT / ".env"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                merged.setdefault(key.strip(), value.strip())
        return cls.from_env(merged)
