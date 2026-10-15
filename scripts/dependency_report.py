"""Report outdated dependencies with the age of their latest release.

Runs inside GitHub Actions on a checked-out repository. Supports npm
(package.json), pip (pyproject.toml, requirements*.txt) and NuGet (*.sln).
Only updates whose latest release is at least MIN_AGE_DAYS old are marked
as ready, so freshly published versions get time to prove themselves.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

NOW = dt.datetime.now(dt.timezone.utc)
MIN_AGE_DAYS = int(os.environ.get("MIN_AGE_DAYS", "30"))
REPO = os.environ.get("REPO", "")
TOKEN = os.environ.get("GH_TOKEN", "")
UA = {"User-Agent": "dependency-report (github.com/ragePolpette)"}


def get_json(url: str, headers: dict[str, str] | None = None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def age_days(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        when = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return (NOW - when).days


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.stdout


rows: list[dict] = []
notes: list[str] = []


def add(eco: str, name: str, current: str, latest: str, released: str | None, kind: str = "") -> None:
    age = age_days(released)
    rows.append(
        {
            "ecosystem": eco,
            "name": name,
            "current": current,
            "latest": latest,
            "released": (released or "")[:10] or "?",
            "age": age,
            "ready": age is not None and age >= MIN_AGE_DAYS,
            "kind": kind,
        }
    )


def semver_kind(current: str, latest: str) -> str:
    cur = re.findall(r"\d+", current or "")
    new = re.findall(r"\d+", latest or "")
    if not cur or not new:
        return ""
    if cur[0] != new[0]:
        return "major"
    if len(cur) > 1 and len(new) > 1 and cur[1] != new[1]:
        return "minor"
    return "patch"


def check_npm() -> None:
    if not Path("package.json").exists():
        return
    out = run(["npm", "outdated", "--json", "--long"])
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        notes.append("npm outdated returned invalid JSON")
        return
    for name, info in sorted(data.items()):
        latest = info.get("latest", "?")
        current = info.get("current") or info.get("wanted") or "missing"
        released = None
        try:
            released = get_json(f"https://registry.npmjs.org/{name}")["time"].get(latest)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"npm registry lookup failed for {name}: {exc}")
        add("npm", name, current, latest, released, semver_kind(current, latest))


def pip_names() -> dict[str, str]:
    specs: dict[str, str] = {}
    pyproject = Path("pyproject.toml")
    if pyproject.exists():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = data.get("project", {})
        deps = list(project.get("dependencies", []))
        for extra in project.get("optional-dependencies", {}).values():
            deps.extend(extra)
        for dep in deps:
            match = re.match(r"\s*([A-Za-z0-9_.\-]+)", dep)
            if match:
                specs[match.group(1).lower()] = dep.strip()
    for req in Path(".").glob("**/requirements*.txt"):
        if "node_modules" in req.parts or ".venv" in req.parts:
            continue
        for line in req.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "-")):
                continue
            match = re.match(r"([A-Za-z0-9_.\-]+)", line)
            if match:
                specs.setdefault(match.group(1).lower(), line)
    return specs


def check_pip() -> None:
    specs = pip_names()
    for name, spec in sorted(specs.items()):
        try:
            data = get_json(f"https://pypi.org/pypi/{name}/json")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"PyPI lookup failed for {name}: {exc}")
            continue
        latest = data["info"]["version"]
        files = data.get("releases", {}).get(latest, [])
        released = files[0].get("upload_time_iso_8601") if files else None
        floor = re.search(r">=\s*([0-9][0-9A-Za-z.\-]*)", spec)
        current = floor.group(1) if floor else spec
        if floor and floor.group(1) == latest:
            continue
        add("pip", name, current, latest, released, semver_kind(current, latest))


def nuget_published(package_id: str, version: str) -> str | None:
    try:
        index = get_json(f"https://api.nuget.org/v3/registration5-semver1/{package_id.lower()}/index.json")
    except Exception:  # noqa: BLE001
        return None
    for page in index.get("items", []):
        items = page.get("items")
        if items is None:
            try:
                items = get_json(page["@id"]).get("items", [])
            except Exception:  # noqa: BLE001
                continue
        for item in items:
            entry = item.get("catalogEntry", {})
            if entry.get("version", "").lower() == version.lower():
                return entry.get("published")
    return None


def check_nuget() -> None:
    solutions = [p for p in Path(".").glob("*.sln")]
    if not solutions:
        return
    target = str(solutions[0])
    subprocess.run(["dotnet", "restore", target], capture_output=True, text=True)
    out = run(["dotnet", "list", target, "package", "--outdated", "--format", "json"])
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        notes.append("dotnet list package returned invalid JSON")
        return
    seen: set[tuple[str, str]] = set()
    for project in data.get("projects", []):
        for framework in project.get("frameworks", []) or []:
            for pkg in framework.get("topLevelPackages", []) or []:
                key = (pkg["id"], pkg.get("latestVersion", ""))
                if key in seen:
                    continue
                seen.add(key)
                current = pkg.get("resolvedVersion") or pkg.get("requestedVersion", "?")
                latest = pkg.get("latestVersion", "?")
                add("nuget", pkg["id"], current, latest, nuget_published(pkg["id"], latest), semver_kind(current, latest))


def dependabot_alerts() -> list[dict]:
    if not (REPO and TOKEN):
        return []
    try:
        return get_json(
            f"https://api.github.com/repos/{REPO}/dependabot/alerts?state=open&per_page=100",
            {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
        )
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Dependabot alerts unavailable: {exc}")
        return []


def main() -> int:
    check_npm()
    check_pip()
    check_nuget()
    alerts = dependabot_alerts()

    lines = [f"## {REPO or Path.cwd().name}", ""]
    if alerts:
        lines.append(f"### Open security alerts: {len(alerts)}")
        lines.append("")
        lines.append("| Severity | Package | Vulnerable range | Fixed in | Summary |")
        lines.append("| --- | --- | --- | --- | --- |")
        for alert in alerts:
            adv = alert.get("security_advisory", {})
            vuln = alert.get("security_vulnerability", {})
            lines.append(
                f"| {adv.get('severity', '?')} | {vuln.get('package', {}).get('name', '?')} | "
                f"{vuln.get('vulnerable_version_range', '?')} | "
                f"{(vuln.get('first_patched_version') or {}).get('identifier', '-')} | "
                f"{adv.get('summary', '').replace('|', '/')} |"
            )
        lines.append("")
    else:
        lines.append("No open security alerts.")
        lines.append("")

    ready = [r for r in rows if r["ready"]]
    waiting = [r for r in rows if not r["ready"]]
    lines.append(f"### Updates ready (latest release at least {MIN_AGE_DAYS} days old): {len(ready)}")
    lines.append("")
    if ready:
        lines.append("| Ecosystem | Package | Current | Latest | Released | Age (days) | Change |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for r in sorted(ready, key=lambda x: (x["kind"] != "major", x["ecosystem"], x["name"])):
            lines.append(
                f"| {r['ecosystem']} | {r['name']} | {r['current']} | {r['latest']} | {r['released']} | {r['age']} | {r['kind']} |"
            )
        lines.append("")
    lines.append(f"### Too recent, waiting: {len(waiting)}")
    lines.append("")
    if waiting:
        lines.append("| Ecosystem | Package | Current | Latest | Released | Age (days) | Change |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for r in sorted(waiting, key=lambda x: (x["ecosystem"], x["name"])):
            age = "?" if r["age"] is None else r["age"]
            lines.append(
                f"| {r['ecosystem']} | {r['name']} | {r['current']} | {r['latest']} | {r['released']} | {age} | {r['kind']} |"
            )
        lines.append("")
    if notes:
        lines.append("### Notes")
        lines.append("")
        lines.extend(f"- {n}" for n in notes)
        lines.append("")

    report = "\n".join(lines)
    Path("dependency-report.md").write_text(report, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
