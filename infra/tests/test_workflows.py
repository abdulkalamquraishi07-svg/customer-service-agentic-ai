"""Security policy for the GitHub Actions workflows (enforced on every CI run)."""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
FILES = sorted(WORKFLOWS.glob("*.yml"))
SHA = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    # YAML 1.1 parses the key `on` as boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def all_steps(workflow: dict):
    for job in workflow.get("jobs", {}).values():
        yield from job.get("steps", [])


def test_workflows_exist():
    assert {p.name for p in FILES} == {"ci.yml", "deploy.yml", "_deploy-env.yml"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_third_party_action_is_pinned_to_a_commit_sha(path):
    for step in all_steps(load(path)):
        uses = step.get("uses")
        if uses and not uses.startswith("./"):
            assert SHA.match(uses), f"{path.name}: {uses} is not pinned to a 40-char commit SHA"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_default_permissions_are_read_only(path):
    perms = load(path).get("permissions")
    assert perms is not None, f"{path.name} must declare top-level permissions"
    assert perms.get("contents") == "read"
    assert set(perms) <= {"contents", "id-token"}


def test_ci_never_requests_an_oidc_token_or_aws_access():
    text = (WORKFLOWS / "ci.yml").read_text()
    wf = load(WORKFLOWS / "ci.yml")
    assert "id-token" not in str(wf.get("permissions"))
    assert "configure-aws-credentials" not in text and "AWS_ROLE" not in text


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_dangerous_triggers_or_stored_credentials(path):
    wf = load(path)
    triggers = wf["on"] if isinstance(wf["on"], dict) else dict.fromkeys(wf["on"])
    assert "pull_request_target" not in triggers  # runs fork code with secrets
    text = path.read_text().lower()
    assert "aws_access_key_id" not in text and "aws_secret_access_key" not in text
    assert "secrets:" not in text  # nothing here needs secrets; OIDC only


def test_checkouts_do_not_persist_credentials():
    for path in FILES:
        for step in all_steps(load(path)):
            if str(step.get("uses", "")).startswith("actions/checkout"):
                assert step["with"]["persist-credentials"] is False


def test_deployments_are_serialized_and_gated_by_environment():
    deploy = load(WORKFLOWS / "deploy.yml")
    assert deploy["concurrency"] == {"group": "deploy", "cancel-in-progress": False}
    jobs = deploy["jobs"]
    assert jobs["deploy-dev"]["needs"] == "ci"  # main is never deployed untested
    assert jobs["staging"]["needs"] == "deploy-dev" and jobs["prod"]["needs"] == "staging"
    assert "ENABLE_STAGING" in jobs["staging"]["if"] and "ENABLE_PROD" in jobs["prod"]["if"]

    env_wf = load(WORKFLOWS / "_deploy-env.yml")
    for job in env_wf["jobs"].values():
        assert "environment" in job  # protection rules / reviewers apply to deploy AND verify


def test_each_job_assumes_the_matching_role_and_prod_skips_test_customers():
    wf = load(WORKFLOWS / "_deploy-env.yml")
    roles = {
        name: [s["with"]["role-to-assume"] for s in job["steps"]
               if "configure-aws-credentials" in str(s.get("uses"))]
        for name, job in wf["jobs"].items()
    }
    assert roles == {
        "deploy": ["${{ vars.AWS_DEPLOY_ROLE_ARN }}"],
        "verify": ["${{ vars.AWS_VERIFY_ROLE_ARN }}"],
    }
    # Smoke tests, canaries and evals create and delete users: never against prod.
    for step in wf["jobs"]["verify"]["steps"]:
        if any(k in step.get("run", "") for k in ("smoke_test", "run_evals", "canary")):
            assert "!= 'prod'" in step["if"], step["name"]


def test_deploy_job_cannot_run_before_verification_of_the_previous_environment():
    deploy = load(WORKFLOWS / "deploy.yml")
    # The reusable workflow runs deploy then verify; the next environment waits for BOTH.
    assert deploy["jobs"]["staging"]["needs"] == "deploy-dev"
    assert load(WORKFLOWS / "_deploy-env.yml")["jobs"]["verify"]["needs"] == "deploy"
