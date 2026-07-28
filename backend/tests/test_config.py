"""Configuration loading.

Worth testing because misconfiguration is the failure mode that wastes the most
time: a Lambda that starts fine and then dies mid-run on a missing variable, or
an embedding dimension that silently disagrees with VECTOR(1024) in the schema.
Fail at startup with the variable named, not at 2 AM inside a nightly run.

`Settings.from_env` takes a plain mapping so these tests never touch os.environ.
"""

import pytest

from brasstacks.config import ConfigError, Settings

BASE = {
    "COCKROACH_DATABASE_URL": "postgresql://u:p@host:26257/defaultdb",
    "AWS_REGION": "us-east-1",
    "MODEL_PROVIDER": "anthropic",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "ANTHROPIC_MODEL_ID": "claude-opus-5",
    "BEDROCK_EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0",
    "EMBEDDING_DIMENSIONS": "1024",
    "S3_ARTIFACT_BUCKET": "bucket",
}


def env(**overrides):
    e = dict(BASE)
    for k, v in overrides.items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return e


class TestHappyPath:
    def test_loads_a_complete_environment(self):
        s = Settings.from_env(env())
        assert s.cockroach_url.startswith("postgresql://")
        assert s.aws_region == "us-east-1"
        assert s.model_provider == "anthropic"
        assert s.embedding_dimensions == 1024

    def test_optional_values_default_to_none(self):
        s = Settings.from_env(env())
        assert s.business_id is None
        assert s.search_api_key is None

    def test_blank_optional_is_treated_as_absent(self):
        # .env files carry empty placeholders like `SEARCH_API_KEY=`. An empty
        # string is not a key.
        s = Settings.from_env(env(SEARCH_API_KEY="", BRASSTACKS_BUSINESS_ID="   "))
        assert s.search_api_key is None
        assert s.business_id is None


class TestRequiredValues:
    @pytest.mark.parametrize("missing", [
        "COCKROACH_DATABASE_URL",
        "AWS_REGION",
        "BEDROCK_EMBEDDING_MODEL_ID",
        "S3_ARTIFACT_BUCKET",
    ])
    def test_missing_required_names_the_variable(self, missing):
        with pytest.raises(ConfigError, match=missing):
            Settings.from_env(env(**{missing: None}))

    def test_blank_required_is_rejected(self):
        with pytest.raises(ConfigError, match="COCKROACH_DATABASE_URL"):
            Settings.from_env(env(COCKROACH_DATABASE_URL="  "))


class TestProviderSelection:
    def test_unknown_provider_is_rejected(self):
        with pytest.raises(ConfigError, match="MODEL_PROVIDER"):
            Settings.from_env(env(MODEL_PROVIDER="openai"))

    def test_provider_defaults_to_anthropic(self):
        s = Settings.from_env(env(MODEL_PROVIDER=None))
        assert s.model_provider == "anthropic"

    def test_provider_is_case_insensitive(self):
        assert Settings.from_env(env(MODEL_PROVIDER="Anthropic")).model_provider == "anthropic"

    def test_anthropic_provider_requires_an_api_key(self):
        with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
            Settings.from_env(env(ANTHROPIC_API_KEY=None))

    def test_bedrock_provider_does_not_require_an_anthropic_key(self):
        # Bedrock auth comes from the AWS credential chain, not a key.
        s = Settings.from_env(env(
            MODEL_PROVIDER="bedrock",
            ANTHROPIC_API_KEY=None,
            BEDROCK_MODEL_ID="anthropic.claude-opus-5",
        ))
        assert s.model_provider == "bedrock"
        assert s.bedrock_model_id == "anthropic.claude-opus-5"

    def test_bedrock_provider_requires_a_bedrock_model_id(self):
        with pytest.raises(ConfigError, match="BEDROCK_MODEL_ID"):
            Settings.from_env(env(MODEL_PROVIDER="bedrock", BEDROCK_MODEL_ID=None))

    def test_reasoning_model_id_resolves_per_provider(self):
        # Callers should not have to branch on the provider to name the model.
        anthropic = Settings.from_env(env())
        assert anthropic.reasoning_model_id == "claude-opus-5"
        bedrock = Settings.from_env(env(
            MODEL_PROVIDER="bedrock", BEDROCK_MODEL_ID="anthropic.claude-opus-5"))
        assert bedrock.reasoning_model_id == "anthropic.claude-opus-5"


class TestEmbeddingDimensions:
    def test_non_numeric_dimensions_are_rejected(self):
        with pytest.raises(ConfigError, match="EMBEDDING_DIMENSIONS"):
            Settings.from_env(env(EMBEDDING_DIMENSIONS="lots"))

    def test_dimensions_must_match_the_schema(self):
        # The schema declares VECTOR(1024). A mismatch here would fail on every
        # insert, deep inside a nightly run, with a confusing pgcode. Catch it
        # at startup instead.
        with pytest.raises(ConfigError, match="1024"):
            Settings.from_env(env(EMBEDDING_DIMENSIONS="512"))

    def test_dimensions_default_to_the_schema_value(self):
        s = Settings.from_env(env(EMBEDDING_DIMENSIONS=None))
        assert s.embedding_dimensions == 1024


class TestSecretsAreNotLeaked:
    def test_repr_masks_the_api_key(self):
        # Settings objects end up in log lines and exception context. A key must
        # not ride along.
        s = Settings.from_env(env(ANTHROPIC_API_KEY="sk-ant-supersecret"))
        assert "supersecret" not in repr(s)

    def test_repr_masks_the_database_password(self):
        s = Settings.from_env(env(
            COCKROACH_DATABASE_URL="postgresql://user:hunter2@host:26257/defaultdb"))
        assert "hunter2" not in repr(s)
