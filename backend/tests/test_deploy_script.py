"""The deploy command, tested where it is easy to get wrong.

Deploying this stack has three sharp edges and all three have drawn blood:

  * `sam deploy --parameter-overrides` re-splits its argument on whitespace, so
    a cron expression arrives as `cron(0` unless literal quotes survive to it.
    That rolled the entire stack back on 2026-08-07.
  * SAM lives at `C:\\Program Files\\Amazon\\AWSSAMCLI\\bin\\sam.cmd`, and any
    route through a shell chokes on the space.
  * The site is a separate step. `sam deploy` alone changes nothing a visitor
    sees, which reads exactly like a failed deploy.

None of that is worth remembering twice, so it lives in one script and the parts
that can be checked without touching AWS are checked here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

import deploy

# samcli/cli/types.py, CfnParameterOverridesType — the shorthand Key=Value form.
_VALUE = r'("(?:\\.|[^"\\]+)*"|\'(?:\\.|[^\'\\]+)*\'|(?:\\.|[^ "\\]+)+)'
SAM_SHORTHAND = r'(?:(?: )([A-Za-z0-9"\']+)=' + _VALUE + r')'


def sam_would_parse(argument: str) -> dict[str, str]:
    """What SAM makes of the string we hand it."""
    return {key: value.strip('"')
            for key, value in re.findall(SAM_SHORTHAND, " " + argument)}


class TestParameterOverrides:
    def test_the_cron_survives_with_its_spaces(self):
        # The regression. `cron(0 18 * * ? *)` has to arrive whole.
        parsed = sam_would_parse(deploy.overrides_argument(
            bucket="brasstacks-artifacts-881550374737",
            schedule_state="ENABLED",
            schedule_expression="cron(0 18 * * ? *)",
        ))
        assert parsed["ScheduleExpression"] == "cron(0 18 * * ? *)"
        assert parsed["ScheduleState"] == "ENABLED"
        assert parsed["ArtifactBucket"] == "brasstacks-artifacts-881550374737"

    def test_an_unquoted_value_is_what_broke_it(self):
        # Kept as a test rather than a comment: this is the exact string the
        # failed deploy sent, and what SAM did with it.
        naive = ('ArtifactBucket=bucket ScheduleState=ENABLED '
                 'ScheduleExpression=cron(0 18 * * ? *)')
        assert sam_would_parse(naive)["ScheduleExpression"] == "cron(0"

    def test_every_value_is_quoted_not_just_the_awkward_one(self):
        # A bucket name cannot contain a space today. Quoting only the value
        # that currently needs it is how this breaks again later.
        argument = deploy.overrides_argument(
            bucket="b", schedule_state="DISABLED",
            schedule_expression="cron(0 6 * * ? *)")
        assert argument.count('="') == 3

    def test_the_schedule_can_be_left_alone(self):
        # Not every deploy is a decision about the nightly spend. With no
        # schedule given, the stack keeps whatever it already has.
        argument = deploy.overrides_argument(bucket="b", schedule_state=None,
                                             schedule_expression=None)
        assert sam_would_parse(argument) == {"ArtifactBucket": "b"}

    def test_the_site_domain_rides_along_when_given(self):
        parsed = sam_would_parse(deploy.overrides_argument(
            bucket="b", schedule_state=None, schedule_expression=None,
            site_domain="trybrasstacks.com",
            site_certificate_arn="arn:aws:acm:us-east-1:1:certificate/x"))
        assert parsed["SiteDomainName"] == "trybrasstacks.com"
        assert parsed["SiteCertificateArn"] == "arn:aws:acm:us-east-1:1:certificate/x"

    def test_an_omitted_domain_leaves_the_stack_alone(self):
        # Same rule as the schedule: `sam deploy` reuses the previous value
        # for any parameter not sent, so a deploy that says nothing about the
        # domain must not mention it — sending "" would detach the alias.
        argument = deploy.overrides_argument(bucket="b", schedule_state=None,
                                             schedule_expression=None)
        assert "SiteDomainName" not in argument
        assert "SiteCertificateArn" not in argument


class TestFindingTheTools:
    def test_a_path_with_spaces_is_returned_whole(self, tmp_path, monkeypatch):
        # Never quoted, never escaped: it goes to subprocess as one argv item
        # with no shell in the way, which is the only thing that reliably works
        # for C:\Program Files\...
        installed = tmp_path / "Program Files" / "sam.cmd"
        installed.parent.mkdir(parents=True)
        installed.write_text("", encoding="utf-8")
        monkeypatch.setattr(deploy.shutil, "which", lambda name: None)

        found = deploy.find_executable("sam", extra=[installed])
        assert found == str(installed)
        assert " " in found          # unquoted, spaces and all

    def test_path_wins_when_it_has_the_tool(self, monkeypatch):
        monkeypatch.setattr(deploy.shutil, "which", lambda name: "/usr/bin/sam")
        assert deploy.find_executable("sam", extra=[Path("/nope")]) == "/usr/bin/sam"

    def test_a_missing_tool_says_what_to_install(self, monkeypatch):
        monkeypatch.setattr(deploy.shutil, "which", lambda name: None)
        with pytest.raises(SystemExit) as raised:
            deploy.find_executable("sam", extra=[Path("/nope")])
        assert "sam" in str(raised.value)


class TestSkippingWorkThatChangedNothing:
    """`sam build` rebuilds sixteen container images. Doing that when no file
    inside one of them changed is most of why deploying felt slow."""

    def _tree(self, root: Path) -> Path:
        (root / "backend" / "src").mkdir(parents=True)
        (root / "backend" / "src" / "app.py").write_text("x", encoding="utf-8")
        (root / "deploy").mkdir()
        (root / "deploy" / "Dockerfile").write_text("FROM base", encoding="utf-8")
        return root

    def test_the_same_tree_fingerprints_the_same(self, tmp_path):
        root = self._tree(tmp_path)
        assert deploy.image_fingerprint(root) == deploy.image_fingerprint(root)

    def test_changing_source_changes_the_fingerprint(self, tmp_path):
        root = self._tree(tmp_path)
        before = deploy.image_fingerprint(root)
        (root / "backend" / "src" / "app.py").write_text("y", encoding="utf-8")
        assert deploy.image_fingerprint(root) != before

    def test_changing_the_dockerfile_changes_it_too(self, tmp_path):
        root = self._tree(tmp_path)
        before = deploy.image_fingerprint(root)
        (root / "deploy" / "Dockerfile").write_text("FROM other", encoding="utf-8")
        assert deploy.image_fingerprint(root) != before

    def test_the_site_is_not_part_of_the_image(self, tmp_path):
        # Editing app.html must never trigger a sixteen-image rebuild.
        root = self._tree(tmp_path)
        before = deploy.image_fingerprint(root)
        (root / "site").mkdir()
        (root / "site" / "app.html").write_text("<html>", encoding="utf-8")
        assert deploy.image_fingerprint(root) == before

    def test_pycache_is_ignored(self, tmp_path):
        # Byte-compiled files change on every run and are not copied into the
        # image; counting them would make every deploy look like a change.
        root = self._tree(tmp_path)
        before = deploy.image_fingerprint(root)
        cache = root / "backend" / "src" / "__pycache__"
        cache.mkdir()
        (cache / "app.cpython-312.pyc").write_text("junk", encoding="utf-8")
        assert deploy.image_fingerprint(root) == before


class TestWhatEachTargetDoes:
    def test_site_is_the_default_and_never_builds_images(self):
        plan = deploy.plan_for("site", backend_changed=True)
        assert plan.publish_site is True
        assert plan.deploy_backend is False

    def test_backend_skips_the_build_when_nothing_changed(self):
        assert deploy.plan_for("backend", backend_changed=False).deploy_backend is False
        assert deploy.plan_for("backend", backend_changed=True).deploy_backend is True

    def test_forcing_it_deploys_anyway(self):
        plan = deploy.plan_for("backend", backend_changed=False, force=True)
        assert plan.deploy_backend is True

    def test_all_does_both(self):
        plan = deploy.plan_for("all", backend_changed=True)
        assert plan.publish_site and plan.deploy_backend


class TestTheOffSwitchActuallyFires:
    """`--schedule DISABLED` is the kill switch for nightly spend.

    The schedule is a CloudFormation parameter, so the only way to change it is
    a stack update — and the fingerprint check exists to skip exactly that when
    no code changed. Left alone, the two combine into an off-switch that
    silently does nothing: the command succeeds, prints nothing alarming, and
    the agents run again at 18:00.
    """

    def test_asking_for_a_schedule_change_forces_the_deploy(self):
        assert deploy.plan_for("auto", backend_changed=False,
                               parameter_change=True).deploy_backend is True

    def test_a_schedule_expression_alone_forces_it_too(self):
        assert deploy.plan_for("site", backend_changed=False,
                               parameter_change=True).deploy_backend is True

    def test_a_domain_change_is_also_a_stack_update(self):
        # The site domain is a CloudFormation parameter exactly like the
        # schedule: without the force, `--site-domain` succeeds, deploys
        # nothing, and the judges keep typing d2iudn7nc8ezqu.cloudfront.net.
        assert deploy.plan_for("site", backend_changed=False,
                               parameter_change=True).deploy_backend is True

    def test_without_a_schedule_change_the_skip_still_applies(self):
        assert deploy.plan_for("auto", backend_changed=False).deploy_backend is False
