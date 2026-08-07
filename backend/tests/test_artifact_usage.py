"""Owner-facing Maker placement metadata is deterministic and bounded."""

from brasstacks.artifact_usage import ARTIFACT_USE_CONTEXTS, artifact_use_context


def test_every_supported_artifact_type_has_a_complete_owner_facing_destination():
    required = {"surface", "placement", "audience", "visibility", "draft_state", "owner_gate", "icon"}

    for artifact_type in ARTIFACT_USE_CONTEXTS:
        context = artifact_use_context(artifact_type)
        assert context["artifact_type"] == artifact_type
        assert required.issubset(context)
        assert all(str(context[field]).strip() for field in required)


def test_google_business_copy_is_explicit_that_the_draft_is_not_public_yet():
    context = artifact_use_context("google_business_post")

    assert context["surface"] == "Google Business Profile"
    assert "public update" in context["placement"]
    assert context["draft_state"] == "Not published"
    assert "confirm publishing" in context["owner_gate"]


def test_unknown_types_fall_back_without_echoing_untrusted_values():
    context = artifact_use_context("../../unknown-provider")

    assert context["artifact_type"] == "general_draft"
    assert context["surface"] == "Owner-selected destination"


def test_stored_presentation_metadata_can_refine_known_fields_but_is_bounded():
    context = artifact_use_context(
        "customer_email",
        stored={
            "surface": "  Approved customer newsletter  ",
            "placement": "x" * 500,
            "unsafe": "must not leak",
        },
    )

    assert context["surface"] == "Approved customer newsletter"
    assert len(context["placement"]) == 280
    assert "unsafe" not in context
