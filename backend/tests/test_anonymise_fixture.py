"""The fixture is committed to a public repository and built from a real
restaurant's data. These tests are the gate that keeps its identity out.

Every test here uses a **synthetic** map. The real one lives in
`.anonymise-map.json`, which is gitignored — a test file that hard-coded the
real business name would republish exactly what the script exists to remove,
which is the bug this module was restructured to fix.
"""

from __future__ import annotations

import json

import pytest

import anonymise_fixture as anon

#: A stand-in with the same shapes as the real map: a multi-word name whose
#: longest form must go first, a domain, an address, a phone and an inbox.
FAKE_MAP = {
    "alias": {"name": "Harborview Japanese", "city": "Harbor Point, CA",
              "username": "owner", "email": "owner@example.com"},
    "replacements": [
        [r"Testco Japanese Cuisine", "Harborview Japanese"],
        [r"Testco", "Harborview"],
        [r"testco", "harborview"],
        [r"testco\.com", "harborview.example.com"],
        [r"77123\s+Sample Street West", "100 Harbor Point Drive"],
        [r"\b77123\b", "100"],
        [r"Sample Street", "Harbor Point Drive"],
        [r"Sampleton", "Harbor Point"],
        [r"\(310\)\s*555[-\s]?1234", "(555) 010-0000"],
        [r"310[-.\s]?555[-.\s]?1234", "555-010-0000"],
        [r"555[-.\s]?1234", "010-0000"],
        [r"real\.owner@gmail\.com", "owner@example.com"],
    ],
    "forbidden": [
        ["business name", r"[Tt]estco"],
        ["street number", r"\b77123\b"],
        ["locality", r"Sampleton|Sample Street"],
        ["phone", r"555[-.\s]?1234"],
        ["owner inbox", r"real\.owner@"],
    ],
}


class TestRedaction:
    def test_the_longest_form_of_a_name_goes_first(self):
        """A three-word name must not become 'Alias Japanese Cuisine' — an
        alias that keeps the original's shape still leaks it."""
        assert anon.redact("Testco Japanese Cuisine", FAKE_MAP) == "Harborview Japanese"

    def test_the_bare_name_is_replaced_in_either_case(self):
        assert "estco" not in anon.redact("Testco and testco", FAKE_MAP)

    def test_the_street_address_goes_in_every_shape(self):
        out = anon.redact("77123 Sample Street West", FAKE_MAP)
        assert "77123" not in out and "Sample Street" not in out

    def test_a_doubled_phone_leaves_nothing_behind(self):
        """The real listings repeat the number back to back, so the fuller rule
        consumes the area code and a bare local part survives. The catch-all
        has to run last."""
        assert "1234" not in anon.redact("(310) 555-1234-555-1234", FAKE_MAP)

    def test_redaction_is_idempotent(self):
        """It runs after every export, and an export may already be clean."""
        once = anon.redact("Testco at 77123 Sample Street West", FAKE_MAP)
        assert anon.redact(once, FAKE_MAP) == once


class TestTheWholeModel:
    @pytest.fixture
    def model(self):
        return {
            "business": {"name": "Testco", "city": "77123 Sample Street West, Sampleton"},
            "owner": {"username": "testco", "display_name": "testco",
                      "email": "real.owner@gmail.com"},
            "finds": [{"title": "Lead with the patio",
                       "rationale": "Testco's website never mentions it.",
                       "predicted_daily_cents": 2200}],
            "evidence": [{"content": "Call Testco on (310) 555-1234.",
                          "source_name": "testco.com"}],
        }

    def test_identity_fields_are_set_not_patched(self, model):
        """`city` held a full street address, so a regex alone would leave a
        plausible-looking address behind."""
        out = anon.anonymise(model, FAKE_MAP)
        assert out["business"]["name"] == "Harborview Japanese"
        assert out["business"]["city"] == "Harbor Point, CA"

    def test_the_owners_real_inbox_never_survives(self, model):
        assert anon.anonymise(model, FAKE_MAP)["owner"]["email"] == "owner@example.com"

    def test_nested_strings_and_keys_are_scrubbed(self, model):
        raw = json.dumps(anon.anonymise(model, FAKE_MAP))
        assert not anon.survivors(raw, FAKE_MAP)

    def test_the_money_and_the_evidence_are_untouched(self, model):
        """Only the identity goes. The finds, the figures and the retrieval
        scores are the real agent's real output and are the whole point."""
        out = anon.anonymise(model, FAKE_MAP)
        assert out["finds"][0]["predicted_daily_cents"] == 2200
        assert out["finds"][0]["title"] == "Lead with the patio"


class TestTheMapIsNotInTheRepository:
    def test_the_example_map_carries_no_real_identifiers(self):
        """The shipped template documents the shape with placeholders. If a
        real value is ever pasted into it, this fails."""
        raw = anon.EXAMPLE_PATH.read_text(encoding="utf-8")
        assert "Real Business" in raw, "the example should look like a template"
        assert "@gmail.com" not in raw

    def test_a_missing_map_fails_with_instructions(self, tmp_path):
        """Better than a traceback for whoever clones this and runs it."""
        with pytest.raises(SystemExit) as caught:
            anon.load_map(tmp_path / "nope.json")
        assert "gitignored" in str(caught.value)


def test_the_committed_fixture_names_nobody():
    """The gate that matters: whatever is in db/fixtures/demo.json right now
    must not identify the real business.

    Skipped without the local map, because the forbidden patterns are the
    identifiers themselves and cannot live in this file.
    """
    try:
        mapping = anon.load_map()
    except SystemExit:
        pytest.skip("no .anonymise-map.json — nothing to check against")
    raw = anon.FIXTURE.read_text(encoding="utf-8")
    left = anon.survivors(raw, mapping)
    assert not left, (
        "db/fixtures/demo.json still identifies the real tenant: "
        + ", ".join(left) + " — run python scripts/anonymise_fixture.py"
    )


def test_source_urls_never_survive_anonymising():
    """A View-source link on an anonymised row would deanonymise the tenant in
    one click, so the scrubber drops the URL fields entirely -- absent, not
    redacted."""
    from anonymise_fixture import scrub

    model = {"finds": [{"evidence": [{
        "content": "a review", "source_url": "https://realplace.example/x",
        "sourceUrl": "https://realplace.example/x", "kind": "review",
    }]}]}
    out = scrub(model, {"replacements": []})
    row = out["finds"][0]["evidence"][0]
    assert "source_url" not in row
    assert "sourceUrl" not in row
    assert row["content"] == "a review"
