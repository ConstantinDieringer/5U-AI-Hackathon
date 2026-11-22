"""Merge a reviewed PR during a safe window. Invoke from a trusted checkout."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess

import httpx

from app import deploy_check

LOCK = "heads/ops/deploy-lock"
FIELDS = "body,state,isDraft,baseRefName,headRefOid,mergeable,statusCheckRollup,isCrossRepository"


def gh(*args: str) -> dict:
    result = subprocess.check_output(["gh", *args], text=True, encoding="utf-8", timeout=45)
    return json.loads(result) if result.strip() else {}


def verify_pr(pr: dict) -> str:
    if (
        pr["state"] != "OPEN"
        or pr["isDraft"]
        or pr["baseRefName"] != "main"
        or pr["isCrossRepository"]
        or pr["mergeable"] != "MERGEABLE"
    ):
        raise ValueError("PR must be open, ready, mergeable and target this repository's main")
    body = re.sub(r"<!--.*?-->", "", pr["body"], flags=re.DOTALL)
    sections = re.split(r"(?m)^##[ \t]+(.+?)\s*$", body)
    evidence = {
        name.casefold(): value.strip() for name, value in zip(sections[1::2], sections[2::2])
    }
    for name in ("Customer behaviour", "Owner and extension point", "Counterexample", "Evidence"):
        if not evidence.get(name.casefold()):
            raise ValueError(f"PR evidence missing: {name}")
    checks = pr["statusCheckRollup"]
    if not any(c.get("name") == "ship-check" and c.get("workflowName") == "CI" for c in checks):
        raise ValueError("CI / ship-check missing for this PR head")
    for check in checks:
        if check.get("__typename") == "StatusContext":
            passed = check.get("state") == "SUCCESS"
        else:
            passed = check.get("status") == "COMPLETED" and check.get("conclusion") == "SUCCESS"
        if not passed:
            raise ValueError(
                f"check pending or failed: {check.get('name') or check.get('context')}"
            )
    return pr["headRefOid"]


def deploy_window(url: str, token: str | None) -> None:
    origin = httpx.URL(url)
    if origin.scheme != "https" or origin.userinfo or not origin.host:
        raise ValueError("PUBLIC_URL must be the trusted production HTTPS origin")
    headers = {"Authorization": "Bearer " + token} if token else {}
    auth = None if token else (os.getenv("UI_USER", "team"), os.environ["UI_PASSWORD"])
    with httpx.Client(base_url=url, headers=headers, auth=auth, timeout=8) as client:
        pricing = client.get("/api/sessions", params={"status": "pricing"})
        pricing.raise_for_status()
        sessions = pricing.json()
        if not isinstance(sessions, list) or sessions:
            raise ValueError("pricing sessions active or unavailable")
        gate = client.get("/api/deploy-gate")
        gate.raise_for_status()
        result = gate.json()
        if result.get("safe") is not True:
            raise ValueError(f"deploy gate: {result.get('reason', 'unknown')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", type=int)
    parser.add_argument("--check", action="store_true", help="verify without claiming or merging")
    args = parser.parse_args()
    number = str(args.pr)
    repo = gh("repo", "view", "--json", "nameWithOwner")["nameWithOwner"]
    endpoint = f"repos/{repo}"
    token = os.getenv("REVIEW_TOKEN")
    if not token and not os.getenv("UI_PASSWORD"):
        raise ValueError("set REVIEW_TOKEN for the production deploy gate")
    url = deploy_check.DEFAULT_URL

    def verify() -> str:
        head = verify_pr(gh("pr", "view", number, "--json", FIELDS))
        comparison = gh("api", f"{endpoint}/compare/main...{head}")
        if comparison["behind_by"]:
            raise ValueError("main advanced; integrate it and rerun ship-check")
        deploy_window(url, token)
        return head

    head = verify()
    if args.check:
        print("MERGE READY", head)
        return
    gh("api", "-X", "POST", endpoint + "/git/refs", "-f", "ref=refs/" + LOCK, "-f", "sha=" + head)
    release = True
    try:
        if verify() != head:
            raise ValueError("PR head changed; review the updated commit")
        # After submitting a merge, a lost HTTP response or deploy timeout is ambiguous.
        # Keep the lock for reconciliation until the exact merged revision is live.
        release = False
        result = gh(
            "api",
            "-X",
            "PUT",
            f"{endpoint}/pulls/{number}/merge",
            "-f",
            "sha=" + head,
            "-f",
            "merge_method=merge",
        )
        if not result.get("merged"):
            release = True
            raise ValueError("GitHub did not merge this PR")
        if not deploy_check.wait_for(result["sha"], url, 600):
            raise ValueError("deployment unverified; retain ops/deploy-lock and inspect Railway")
        release = True
    finally:
        if release:
            gh("api", "-X", "DELETE", endpoint + "/git/refs/" + LOCK)
        else:
            print("Deploy lock retained. Reconcile the PR and live SHA before releasing it.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, httpx.HTTPError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"MERGE BLOCKED: {exc}") from None
