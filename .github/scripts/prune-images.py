#!/usr/bin/env python3
"""Delete old commit-sha image versions from GHCR, and nothing else.

Versions accumulate and nobody removes them; a registry with no retention
policy is a disk bill and a list nobody can read. But a careless prune is
worse than none, so this refuses far more than it removes.

WHAT IT NEVER TOUCHES, and why:

  untagged versions   A multi-architecture image is an INDEX whose children -
                      the amd64 and arm64 manifests - carry no tag of their
                      own. "Delete everything untagged" is the advice you find
                      first, and it silently breaks every multi-arch tag in
                      the registry by deleting the manifests they point at.
  sha256-* tags       cosign stores a signature under the digest of what it
                      signed. Delete it and the image is still pullable and no
                      longer verifiable, which is the worst of both.
  v* tags             Releases. They are the point.
  main, latest        Moving tags that something may be pulling right now.

WHAT IT DELETES: versions tagged with a 40-character commit sha, beyond the
most recent --keep of them. Those are the per-commit builds; the newest few
are worth keeping so a rollback has somewhere to go.

  python3 prune-images.py --owner X --package Y            # say what it would do
  python3 prune-images.py --owner X --package Y --apply    # do it
  python3 prune-images.py --self-test                      # check the policy
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

API = "https://api.github.com"
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SIGNATURE = re.compile(r"^sha256-[0-9a-f]{64}")
RELEASE = re.compile(r"^v[0-9]")
MOVING = {"main", "latest"}

# One version as the packages API returns it.
Version = dict[str, Any]


def protected(tags: list[str]) -> str | None:
    """Why this version must be kept, or None if it may go."""
    if not tags:
        return "untagged: a child of some multi-arch index"
    for tag in tags:
        if SIGNATURE.match(tag):
            return "a cosign signature"
        if RELEASE.match(tag):
            return "a release (" + tag + ")"
        if tag in MOVING:
            return "a moving tag (" + tag + ")"
    if not all(COMMIT_SHA.match(tag) for tag in tags):
        return "tagged with something this policy does not recognise"
    return None


def select(versions: list[Version], keep: int) -> tuple[list[Version], list[tuple[Version, str]]]:
    """Split versions into (to delete, [(kept, reason)]). Newest first."""
    ordered = sorted(versions, key=lambda v: v["created_at"], reverse=True)
    delete: list[Version] = []
    kept: list[tuple[Version, str]] = []
    seen = 0
    for version in ordered:
        tags = version.get("metadata", {}).get("container", {}).get("tags", [])
        reason = protected(tags)
        if reason:
            kept.append((version, reason))
            continue
        seen += 1
        if seen <= keep:
            kept.append((version, "one of the " + str(keep) + " most recent commit builds"))
        else:
            delete.append(version)
    return delete, kept


def request(method: str, path: str, token: str) -> Any:
    req = urllib.request.Request(  # noqa: S310
        API + path,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    # The URL is API + a path built here, never anything a caller supplies,
    # so the scheme cannot become file: or anything else.
    with urllib.request.urlopen(req) as response:  # noqa: S310
        if response.status == 204:
            return None
        return json.load(response)


def fetch(owner: str, package: str, token: str) -> list[Version]:
    versions: list[Version] = []
    page = 1
    while True:
        path = (
            "/users/" + owner + "/packages/container/" + package + "/versions"
            "?per_page=100&page=" + str(page)
        )
        batch = request("GET", path, token)
        if not batch:
            return versions
        versions.extend(batch)
        page += 1


def self_test() -> int:
    """The policy, on the cases that would cost the most to get wrong."""

    def version(identifier: int, date: str, *tags: str) -> Version:
        return {
            "id": identifier,
            "created_at": date,
            "metadata": {"container": {"tags": list(tags)}},
        }

    versions = [
        version(1, "2026-09-01T00:00:00Z", "v0.1.0", "a" * 40),
        version(2, "2026-09-02T00:00:00Z"),
        version(3, "2026-09-03T00:00:00Z", "sha256-" + "b" * 64),
        version(4, "2026-09-04T00:00:00Z", "main", "c" * 40),
        version(5, "2026-09-05T00:00:00Z", "d" * 40),
        version(6, "2026-09-06T00:00:00Z", "e" * 40),
        version(7, "2026-09-07T00:00:00Z", "f" * 40),
    ]
    delete, kept = select(versions, keep=2)
    deleted_ids = sorted(v["id"] for v in delete)

    failures = 0

    def check(condition: bool, description: str) -> None:
        nonlocal failures
        print(("  ok   " if condition else "  FAIL ") + description)
        failures += 0 if condition else 1

    check(deleted_ids == [5], "only the oldest surplus commit build goes, got " + str(deleted_ids))
    check(all(v["id"] != 2 for v in delete), "an untagged version is never deleted")
    check(all(v["id"] != 3 for v in delete), "a cosign signature is never deleted")
    check(all(v["id"] != 1 for v in delete), "a release is never deleted")
    check(all(v["id"] != 4 for v in delete), "a moving tag is never deleted")
    check(len(kept) == len(versions) - len(delete), "every version is either kept or deleted")

    empty, _ = select([], keep=2)
    check(empty == [], "an empty registry deletes nothing")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Prune old GHCR image versions.")
    parser.add_argument("--owner")
    parser.add_argument("--package")
    parser.add_argument("--keep", type=int, default=5)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        print("policy self-test")
        failures = self_test()
        print("  ---", "all good" if not failures else str(failures) + " FAILED")
        return 1 if failures else 0

    if not (args.owner and args.package):
        parser.error("--owner and --package are required")

    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("GH_TOKEN is empty; nothing to do.", file=sys.stderr)
        return 1

    try:
        versions = fetch(args.owner, args.package, token)
    except urllib.error.HTTPError as error:
        print(args.package + ": HTTP " + str(error.code) + " listing versions", file=sys.stderr)
        return 1

    delete, kept = select(versions, args.keep)
    print(
        args.package + ": " + str(len(versions)) + " versions, keeping "
        + str(len(kept)) + ", deleting " + str(len(delete))
    )
    for version_kept, reason in kept:
        tags = version_kept["metadata"]["container"]["tags"] or ["<untagged>"]
        print("    keep   " + ",".join(tags)[:58].ljust(60) + reason)
    for version_gone in delete:
        tags = version_gone["metadata"]["container"]["tags"]
        if not args.apply:
            print("    WOULD DELETE " + ",".join(tags)[:58])
            continue
        request(
            "DELETE",
            "/users/" + args.owner + "/packages/container/" + args.package
            + "/versions/" + str(version_gone["id"]),
            token,
        )
        print("    deleted " + ",".join(tags)[:58])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
