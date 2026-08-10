#!/usr/bin/env python3
"""Resolve AAP environment credentials, verify a Dispatcher JT, launch, and wait."""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import yaml


class DispatcherLaunchError(Exception):
    """Fail-closed Dispatcher launch error."""


def _load_route(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        route = json.load(handle)
    if not isinstance(route, dict):
        raise DispatcherLaunchError("Dispatcher route must be a JSON object")
    return route


def _target(route: dict[str, Any]) -> tuple[str, str]:
    env = route.get("target_env")
    if not isinstance(env, str) or not env:
        raise DispatcherLaunchError("Dispatcher route target_env is required")
    try:
        targets = json.loads(os.environ.get("AAP_ENV_TARGETS_JSON", ""))
    except json.JSONDecodeError as exc:
        raise DispatcherLaunchError("AAP_ENV_TARGETS_JSON must be valid JSON") from exc
    if not isinstance(targets, dict) or not isinstance(targets.get(env), dict):
        raise DispatcherLaunchError(f"No AAP target is configured for environment '{env}'")
    target = targets[env]
    if target.get("username") or target.get("password"):
        raise DispatcherLaunchError("username/password targets are rejected; bearer token only")
    host = target.get("host")
    token = target.get("token")
    if not isinstance(host, str) or not host.strip() or not isinstance(token, str) or not token:
        raise DispatcherLaunchError(f"AAP target '{env}' requires host and token")
    host = host.strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "https://" + host
    return host, token


def _request(host: str, token: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        host + path,
        data=body,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=60, context=context) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise DispatcherLaunchError(f"AAP API HTTP {exc.code} for {path}") from exc
    except urllib.error.URLError as exc:
        raise DispatcherLaunchError(f"AAP API connection failed for {path}: {exc.reason}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DispatcherLaunchError(f"AAP API returned invalid JSON for {path}") from exc
    if not isinstance(data, dict):
        raise DispatcherLaunchError(f"AAP API returned an invalid object for {path}")
    return data


def _extra_vars(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = yaml.safe_load(value) or {}
        if isinstance(loaded, dict):
            return loaded
    raise DispatcherLaunchError("Dedicated Dispatcher JT extra_vars must be a mapping")


def _lookup_and_verify_jt(
    host: str, token: str, route: dict[str, Any]
) -> dict[str, Any]:
    name = route.get("job_template")
    if not isinstance(name, str) or not name:
        raise DispatcherLaunchError("Dispatcher route job_template is required")
    query = urllib.parse.urlencode({"name": name})
    payload = _request(host, token, f"/api/controller/v2/job_templates/?{query}")
    results = payload.get("results")
    count = payload.get("count")
    if not isinstance(results, list) or count != 1 or len(results) != 1:
        raise DispatcherLaunchError(
            f"Dispatcher JT '{name}' must resolve to exactly one object on {host}"
        )
    jt = results[0]
    if not isinstance(jt, dict) or jt.get("name") != name:
        raise DispatcherLaunchError(f"Dispatcher JT identity mismatch for '{name}'")
    if jt.get("allow_simultaneous", False):
        raise DispatcherLaunchError(f"Dispatcher JT '{name}' must have allow_simultaneous=false")

    if route.get("dedicated") is True:
        if jt.get("ask_variables_on_launch", False) or jt.get("survey_enabled", False):
            raise DispatcherLaunchError(
                f"Tenant-bound Dispatcher JT '{name}' must have no variable prompt or survey"
            )
        actual = _extra_vars(jt.get("extra_vars", {}))
        expected = route.get("fixed_extra_vars")
        if not isinstance(expected, dict):
            raise DispatcherLaunchError("Dedicated Dispatcher route lacks fixed_extra_vars")
        mismatches = [key for key, value in expected.items() if actual.get(key) != value]
        if mismatches:
            raise DispatcherLaunchError(
                f"Tenant-bound Dispatcher JT '{name}' binding mismatch: "
                + ", ".join(sorted(mismatches))
            )
    return jt


def _shared_launch_vars(route: dict[str, Any]) -> dict[str, Any]:
    extra = {
        "target_env": route["target_env"],
        "dispatch_scope": route["dispatch_scope"],
        "trigger_commit": os.environ.get("TRIGGER_COMMIT", ""),
        "trigger_source": os.environ.get("TRIGGER_SOURCE", "ci-cd-pipeline"),
        "control_revision": os.environ.get("CONTROL_REVISION", ""),
    }
    for field in ("tenant_id", "triggered_repo"):
        if route.get(field):
            extra[field] = route[field]
    return extra


def launch_and_wait(route: dict[str, Any], timeout_minutes: int) -> int:
    host, token = _target(route)
    jt = _lookup_and_verify_jt(host, token, route)
    payload: dict[str, Any] = {}
    if route.get("dedicated") is not True:
        payload["extra_vars"] = json.dumps(_shared_launch_vars(route))
    launch = _request(
        host,
        token,
        f"/api/controller/v2/job_templates/{jt['id']}/launch/",
        payload,
    )
    job_id = launch.get("id")
    if not isinstance(job_id, int):
        raise DispatcherLaunchError(
            f"Dispatcher JT '{route['job_template']}' launch did not return a job id"
        )
    print(
        f"Dispatcher launched: id={job_id}, env={route['target_env']}, "
        f"scope={route['dispatch_scope']}, jt={route['job_template']}"
    )
    checks = max(1, timeout_minutes * 6)
    for _ in range(checks):
        time.sleep(10)
        status = _request(host, token, f"/api/controller/v2/jobs/{job_id}/").get("status")
        if status == "successful":
            print(f"Dispatcher job {job_id} completed successfully")
            return job_id
        if status in {"failed", "error", "canceled"}:
            raise DispatcherLaunchError(f"Dispatcher job {job_id} ended {status}")
    raise DispatcherLaunchError(
        f"Dispatcher job {job_id} did not complete within {timeout_minutes} minutes"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", required=True)
    parser.add_argument("--timeout-minutes", type=int, default=30)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        launch_and_wait(_load_route(args.route), args.timeout_minutes)
    except Exception as exc:  # noqa: BLE001 - pipeline entrypoint fails closed
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
