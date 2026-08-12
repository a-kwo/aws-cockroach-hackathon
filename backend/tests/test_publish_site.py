"""The local publish path has to build the same site the CI job builds.

`scripts/publish_site.py` is now the primary way the board reaches S3, and it is
easy for it to drift quietly behind `.github/workflows/deploy-frontend.yml`:
`build_web.py` derives most endpoints from the decision endpoint, so a missing
variable produces a site that works — minus one feature — rather than an error.

`GOOGLE_OAUTH_ENABLED` is the one with no fallback at all. Omit it and the page
ships with no Sign in with Google button, which is indistinguishable from the
feature having been turned off.
"""

from __future__ import annotations

import re
from pathlib import Path

import publish_site

OUTPUTS = {
    "DecisionEndpoint": "https://api.example.com/v1",
    "WorkflowEndpoint": "https://api.example.com/v1/workflow",
    "OnboardingEndpoint": "https://api.example.com/v1/onboarding",
    "LoginEndpoint": "https://api.example.com/v1/login",
    "RegisterEndpoint": "https://api.example.com/v1/register",
    "RunEndpoint": "https://api.example.com/v1/run",
    "ProfileEndpoint": "https://api.example.com/v1/profile",
    "AdminWorkspacesEndpoint": "https://api.example.com/v1/admin/workspaces",
    "GoogleStartEndpoint": "https://api.example.com/v1/auth/google/start",
    "GoogleCompleteEndpoint": "https://api.example.com/v1/auth/google/complete",
    "SiteBucketName": "site-bucket",
    "SiteUrl": "https://d2iudn7nc8ezqu.cloudfront.net",
}

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy-frontend.yml"


def test_every_endpoint_the_ci_job_sets_is_set_here_too():
    """Parity, checked against the workflow rather than against a memory of it.

    The CI job is the specification: whatever it hands `build_web.py`, the local
    publish has to hand it too, or the two produce different sites from one
    commit.
    """
    ci_vars = set(re.findall(r"^\s{10}([A-Z0-9_]+):\s*\$\{\{",
                             WORKFLOW.read_text(encoding="utf-8"), re.M))
    assert "GOOGLE_OAUTH_ENABLED" in ci_vars, "workflow shape changed"

    local = publish_site.frontend_env(OUTPUTS, oauth_enabled=True)
    missing = ci_vars - set(local)
    assert not missing, f"publish_site.py does not set: {sorted(missing)}"


def test_the_google_button_is_drawn_only_when_the_client_exists():
    # No OAuth client means the routes answer 404, and a button that 404s is
    # worse than no button. Same switch, same reason, as the CI job.
    on = publish_site.frontend_env(OUTPUTS, oauth_enabled=True)
    off = publish_site.frontend_env(OUTPUTS, oauth_enabled=False)

    assert on["GOOGLE_OAUTH_ENABLED"] == "1"
    assert off["GOOGLE_OAUTH_ENABLED"] == ""


def test_emoji_survive_the_windows_console():
    # Every find carries an emoji and this script is run from a Windows shell,
    # where cp1252 cannot encode one. Without this the build dies mid-publish.
    assert publish_site.frontend_env(OUTPUTS, oauth_enabled=False)[
        "PYTHONIOENCODING"] == "utf-8"


def test_a_fresh_export_is_anonymised_before_anything_is_uploaded():
    """Exporting without anonymising publishes the real tenant's identity.

    The committed fixture is scrubbed, but `export_fixture.py` writes whatever
    tenant BRASSTACKS_BUSINESS_ID points at, verbatim. The export path must
    therefore rewrite the fixture through the anonymise map and then prove the
    scrub took (`--check`) before a single byte reaches S3 — and if the map is
    absent, anonymise_fixture exits with instructions, which aborts the publish
    rather than shipping a real business's reviews and revenue to the demo URL.
    """
    commands = publish_site.export_commands()
    scripts = [Path(command[1]).name for command in commands]

    assert scripts[0] == "export_fixture.py"
    assert scripts[1] == "anonymise_fixture.py"
    assert scripts[2] == "anonymise_fixture.py"
    assert commands[1][-1] != "--check", "rewrite first, then verify"
    assert commands[2][-1] == "--check"


def test_a_missing_output_becomes_empty_rather_than_the_string_none():
    # build_web treats "" as absent and derives a fallback. "None" — which is
    # what the AWS CLI prints for a missing output — would be spliced into the
    # page as a URL.
    sparse = publish_site.frontend_env(
        {"DecisionEndpoint": "https://api.example.com/v1"}, oauth_enabled=False)
    assert sparse["WORKFLOW_API_ENDPOINT"] == ""
    assert "None" not in sparse.values()
