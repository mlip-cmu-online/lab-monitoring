#!/usr/bin/env python3
"""Generate the Lab 8 report and manifest from durable monitoring evidence."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


REPORT_NAME = "monitoring-report.html"
MANIFEST_NAME = "monitoring-manifest.json"
CHECKER_VERSION = "1.0"
REVISION_PATHS = (
    "docker-compose.yaml",
    "kafka-monitoring.py",
    "prometheus/prometheus.yml",
)
SECRET_PATTERNS = (
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|token)\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
    re.compile(r"\b(?:ghp|github_pat|sk|xox[baprs])_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)-----BEGIN (?:OPENSSH|RSA|EC|DSA) PRIVATE KEY-----"),
    re.compile(r"https?://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE),
)
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
REQUIRED_QUERIES = {
    "total_successful_requests": 'sum(request_count_total{http_status="200"})',
    "request_rate": "sum(rate(request_count_total[5m]))",
    "node_cpu_usage": 'sum by (instance) (rate(node_cpu_seconds_total{mode="system"}[5m]))',
    "p95_request_latency": "histogram_quantile(0.95, sum by (le) (rate(request_latency_seconds_bucket[5m])))",
}


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, check=False, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def parse_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(read_text(path))
    except (json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def check(name: str, present: bool, location: str, identifier: str, detail: str) -> dict[str, str]:
    return {
        "name": name,
        "status": "present" if present else "missing",
        "location": location,
        "identifier": identifier,
        "detail": detail,
    }


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def sanitize_repository_url(value: str) -> tuple[str, bool]:
    value = value.strip()
    scp_match = re.fullmatch(r"git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?", value)
    if scp_match:
        owner, repository = scp_match.groups()
        return f"https://github.com/{owner}/{repository.removesuffix('.git')}", True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "invalid-url-redacted", False
    host = parsed.hostname or ""
    parts = [part for part in parsed.path.removesuffix(".git").split("/") if part]
    valid = (
        parsed.scheme == "https" and host.casefold() == "github.com"
        and parsed.username is None and parsed.password is None
        and not parsed.query and not parsed.fragment and len(parts) == 2
    )
    if not valid:
        return "invalid-url-redacted", False
    return f"https://github.com/{parts[0]}/{parts[1]}", True


def local_display_path(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return path.name


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_query(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", "", value).replace("'", '"').casefold()


def completed_compose_and_prometheus(compose: str, prometheus: str) -> tuple[bool, str]:
    compose_plain = "\n".join(line.split("#", 1)[0] for line in compose.splitlines())
    compose_patterns = (
        r"(?m)^\s*services\s*:\s*$",
        r"(?m)^\s+prometheus\s*:\s*$",
        r"(?m)^\s+node-exporter\s*:\s*$",
        r"(?m)^\s+grafana\s*:\s*$",
        r"(?m)^\s+pid\s*:\s*host\s*$",
        r"--path\.rootfs=/host",
        r"/\s*:\s*/host\s*:ro",
        r"host\.docker\.internal\s*:\s*host-gateway",
        r"9090\s*:\s*9090",
        r"9100\s*:\s*9100",
        r"3000\s*:\s*3000",
    )
    prometheus_patterns = (
        r"job_name\s*:\s*['\"]?prometheus",
        r"job_name\s*:\s*['\"]?kafka-monitoring",
        r"host\.docker\.internal:8765",
        r"job_name\s*:\s*['\"]?node-exporter",
        r"node-exporter:9100",
    )
    ok = all(re.search(pattern, compose_plain, re.IGNORECASE) for pattern in compose_patterns)
    ok = ok and all(re.search(pattern, prometheus, re.IGNORECASE) for pattern in prometheus_patterns)
    detail = (
        "Compose defines Prometheus, Grafana, host-scoped node exporter, and all ports; Prometheus uses the expected host-gateway and service-DNS targets."
        if ok else
        "Complete the three Compose services, host-scoped node-exporter configuration, ports, and both expected Prometheus network targets."
    )
    return ok, detail


def completed_metrics_code(source: str) -> tuple[bool, str]:
    executable = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    patterns = (
        r"(?m)^\s*topic\s*=\s*['\"][^'\"]+['\"]",
        r"Counter\s*\([^)]*['\"]request_count['\"][^)]*\[\s*['\"]http_status['\"]\s*\]",
        r"Histogram\s*\([^)]*['\"]request_latency_seconds['\"]",
        r"REQUEST_COUNT\.labels\s*\(\s*http_status\s*=\s*[^)]+\)\.inc\s*\(",
        r"REQUEST_LATENCY\.observe\s*\(",
    )
    ok = all(re.search(pattern, executable, re.IGNORECASE | re.DOTALL) for pattern in patterns)
    ok = ok and "..." not in executable
    detail = (
        "The committed monitoring script defines a topic, the status-labeled counter, the latency histogram, and updates both metrics."
        if ok else
        "Define the team topic and replace the counter/latency placeholders with executable labeled metric updates."
    )
    return ok, detail


def valid_metrics_output(text: str) -> tuple[bool, str]:
    required = (
        "request_count_total",
        "request_latency_seconds_bucket",
        "request_latency_seconds_count",
        "request_latency_seconds_sum",
        "http_status=",
    )
    ok = all(term in text for term in required) and bool(re.search(r'request_count_total\{[^\n]*http_status="\d{3}"', text))
    return ok, (
        "Saved application output contains the labeled request counter and complete latency histogram series."
        if ok else
        "Save /metrics output containing request_count_total with an http_status label and the latency bucket, count, and sum series."
    )


def valid_targets(value: dict[str, Any] | None) -> tuple[bool, list[str], str]:
    active = value.get("data", {}).get("activeTargets", []) if value else []
    if not isinstance(active, list):
        return False, [], "The targets export does not contain data.activeTargets."
    found: dict[str, dict[str, Any]] = {}
    for target in active:
        if isinstance(target, dict):
            labels = target.get("labels", {})
            job = labels.get("job") if isinstance(labels, dict) else None
            if isinstance(job, str):
                found[job] = target
    expected_addresses = {
        "prometheus": "localhost:9090",
        "kafka-monitoring": "host.docker.internal:8765",
        "node-exporter": "node-exporter:9100",
    }
    ok = True
    for job, address in expected_addresses.items():
        target = found.get(job, {})
        ok = ok and target.get("health") == "up" and address in str(target.get("scrapeUrl", ""))
    jobs = sorted(job for job in expected_addresses if job in found)
    detail = (
        "The saved target response shows all three expected jobs UP on the intended network addresses."
        if ok else
        "Save a Prometheus targets response with prometheus, kafka-monitoring, and node-exporter UP on their expected addresses."
    )
    return ok, jobs, detail


def query_records(value: dict[str, Any] | None) -> tuple[dict[str, dict[str, Any]], str, str]:
    if value is None or not isinstance(value.get("queries"), list):
        return {}, "", ""
    records: dict[str, dict[str, Any]] = {}
    for item in value["queries"]:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            records[item["name"]] = item
    return records, str(value.get("repository_commit", "")).strip(), str(value.get("dashboard_uid", "")).strip()


def valid_query_results(value: dict[str, Any] | None) -> tuple[bool, dict[str, int], str, str, str]:
    records, commit, dashboard_uid = query_records(value)
    panel_ids: dict[str, int] = {}
    ok = bool(commit and dashboard_uid)
    for name, expected in REQUIRED_QUERIES.items():
        record = records.get(name, {})
        response = record.get("response", {})
        data = response.get("data", {}) if isinstance(response, dict) else {}
        result = data.get("result", []) if isinstance(data, dict) else []
        panel_id = record.get("panel_id")
        record_ok = (
            normalize_query(record.get("query")) == normalize_query(expected)
            and response.get("status") == "success"
            and isinstance(result, list) and bool(result)
            and isinstance(panel_id, int) and not isinstance(panel_id, bool)
        )
        if record_ok:
            panel_ids[name] = panel_id
        ok = ok and record_ok
    ok = ok and len(set(panel_ids.values())) == len(REQUIRED_QUERIES)
    detail = (
        "All four named PromQL expressions have nonempty saved results and distinct panel IDs."
        if ok else
        "Record the four exact named queries, nonempty successful Prometheus responses, repository commit, dashboard UID, and distinct panel IDs."
    )
    return ok, panel_ids, commit, dashboard_uid, detail


def dashboard_object(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    nested = value.get("dashboard")
    return nested if isinstance(nested, dict) else value


def flatten_panels(panels: Any) -> list[dict[str, Any]]:
    if not isinstance(panels, list):
        return []
    flat: list[dict[str, Any]] = []
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        nested = panel.get("panels")
        if isinstance(nested, list):
            flat.extend(flatten_panels(nested))
        if isinstance(panel.get("targets"), list):
            flat.append(panel)
    return flat


def valid_dashboard(value: dict[str, Any] | None) -> tuple[bool, dict[str, int], str, str]:
    dashboard = dashboard_object(value)
    if dashboard is None:
        return False, {}, "", "The dashboard export is not a JSON object."
    uid = str(dashboard.get("uid", "")).strip()
    panels = flatten_panels(dashboard.get("panels"))
    matched: dict[str, int] = {}
    for panel in panels:
        panel_id = panel.get("id")
        title = panel.get("title")
        expressions = {
            normalize_query(target.get("expr"))
            for target in panel.get("targets", []) if isinstance(target, dict)
        }
        if not isinstance(panel_id, int) or isinstance(panel_id, bool) or not isinstance(title, str) or not title.strip():
            continue
        for name, expected in REQUIRED_QUERIES.items():
            if normalize_query(expected) in expressions:
                matched[name] = panel_id
    ok = bool(uid) and len(matched) == len(REQUIRED_QUERIES) and len(set(matched.values())) == len(REQUIRED_QUERIES)
    detail = (
        "The exported dashboard has a UID and four titled, distinct panels containing the required PromQL expressions."
        if ok else
        "Export a saved dashboard with a UID and four titled, distinct panels using the required PromQL expressions."
    )
    return ok, matched, uid, detail


def valid_slo_rule(value: dict[str, Any] | None) -> tuple[bool, str, int | None, str, str, str]:
    if value is None:
        return False, "", None, "", "", "The operational-rule file is not a JSON object."
    dashboard_uid = str(value.get("dashboard_uid", "")).strip()
    commit = str(value.get("repository_commit", "")).strip()
    panel_id = value.get("panel_id")
    threshold = value.get("threshold_seconds")
    duration = str(value.get("duration", "")).strip()
    owner = str(value.get("owner", "")).strip()
    first_action = str(value.get("first_action", "")).strip()
    ok = (
        bool(dashboard_uid and commit and owner and first_action)
        and isinstance(panel_id, int) and not isinstance(panel_id, bool)
        and isinstance(threshold, (int, float)) and not isinstance(threshold, bool) and threshold > 0
        and bool(re.fullmatch(r"[1-9]\d*\s*(?:s|m|h|seconds?|minutes?|hours?)", duration, re.IGNORECASE))
    )
    summary = f"p95 > {threshold}s for {duration}" if ok else "invalid operational rule"
    detail = (
        "The p95 rule records a positive threshold, alert duration, owner, and first action."
        if ok else
        "Record repository_commit, dashboard_uid, p95 panel_id, positive threshold_seconds, duration, owner, and first_action."
    )
    return ok, dashboard_uid, panel_id if isinstance(panel_id, int) else None, commit, summary, detail


def image_data_uri(path: Path) -> tuple[str, bool]:
    mime_type = IMAGE_TYPES.get(path.suffix.casefold())
    if mime_type is None or not path.is_file() or path.stat().st_size == 0:
        return "", False
    content = path.read_bytes()
    signatures = {
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
    }
    if not signatures[mime_type]:
        return "", False
    return f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}", True


def evidence_section(title: str, location: str, content: str, withheld: bool) -> str:
    body = "Content withheld because an obvious credential pattern was detected." if withheld else content
    return (
        f'<details><summary>{html.escape(title)} — <code>{html.escape(location)}</code></summary>'
        f'<pre>{html.escape(body)}</pre></details>'
    )


def render_report(
    learner: str,
    generated_at: str,
    repository_url: str,
    repository_valid: bool,
    commit_sha: str,
    checks: list[dict[str, str]],
    evidence: list[tuple[str, str, str]],
    withheld: set[str],
    screenshot_uri: str,
) -> str:
    overall = "complete" if all(item["status"] == "present" for item in checks) else "incomplete"
    rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(item['name'])}</td>"
        f"<td class=\"{item['status']}\">{item['status']}</td>"
        f"<td>{html.escape(item['identifier'])}</td>"
        f"<td>{html.escape(item['detail'])}</td>"
        "</tr>"
        for item in checks
    )
    commit_url = f"{repository_url}/commit/{commit_sha}" if repository_valid else repository_url
    sections = "\n".join(
        evidence_section(title, location, content, location in withheld)
        for title, location, content in evidence
    )
    screenshot = (
        f'<img src="{screenshot_uri}" alt="Saved Grafana dashboard with the four required panels">'
        if screenshot_uri else "<p>The supplied dashboard screenshot could not be embedded.</p>"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lab 8 Monitoring Submission Report</title>
  <style>
    body {{ color:#17202a; font:16px/1.45 system-ui,sans-serif; margin:2rem auto; max-width:76rem; padding:0 1rem; }}
    h1,h2 {{ color:#102a43; }} table {{ border-collapse:collapse; width:100%; }}
    th,td {{ border:1px solid #bcccdc; padding:.55rem; text-align:left; vertical-align:top; }} th {{ background:#f0f4f8; }}
    pre {{ background:#f5f7fa; border:1px solid #bcccdc; overflow:auto; padding:1rem; white-space:pre-wrap; }}
    code {{ overflow-wrap:anywhere; }} details {{ margin:.7rem 0; }} summary {{ cursor:pointer; font-weight:650; }}
    img {{ border:1px solid #bcccdc; height:auto; max-width:100%; }}
    .present,.complete {{ color:#176b3a; font-weight:700; }} .missing,.incomplete {{ color:#a61b1b; font-weight:700; }}
    .note {{ background:#fffbea; border-left:.3rem solid #d69e2e; padding:.7rem 1rem; }}
  </style>
</head>
<body>
  <h1>Lab 8: Monitoring Submission Report</h1>
  <table>
    <tr><th>Learner</th><td>{html.escape(learner)}</td></tr>
    <tr><th>Generated</th><td>{html.escape(generated_at)}</td></tr>
    <tr><th>Overall completeness</th><td class="{overall}">{overall}</td></tr>
    <tr><th>Immutable repository revision</th><td><a href="{html.escape(commit_url)}">{html.escape(commit_url)}</a></td></tr>
    <tr><th>Commit SHA</th><td><code>{html.escape(commit_sha)}</code></td></tr>
    <tr><th>Checker version</th><td>{CHECKER_VERSION}</td></tr>
  </table>

  <h2>Completeness</h2>
  <table><thead><tr><th>Required evidence</th><th>Status</th><th>Identifier</th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table>

  <h2>Grafana dashboard screenshot</h2>
  {screenshot}

  <h2>Committed configuration and saved evidence</h2>
  {sections}

  <h2>Staff spot checks</h2>
  <p class="note">This checker confirms objective completeness and identifier consistency only. Answer these separately in Canvas; it does not interpret or score either answer.</p>
  <ol>
    <li><strong>Counter change:</strong> explain one observed request_count_total change, the effect of a restart, and why rate() precedes aggregation.</li>
    <li><strong>p95:</strong> explain the le aggregation and interpret the saved threshold, duration, owner, and first action.</li>
  </ol>

  <h2>Safety and durability</h2>
  <p>The report embeds the committed source, saved responses, dashboard export, operational rule, and screenshot. No Kafka, Prometheus, or Grafana process is contacted or started. The checker scans included text for common plaintext credential patterns; review the screenshot and finished report yourself before uploading.</p>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the Lab 8 HTML report and JSON manifest without contacting Kafka, Prometheus, or Grafana."
    )
    parser.add_argument("--learner", required=True)
    parser.add_argument("--repository-url", required=True, help="HTTPS or git@github.com repository URL")
    parser.add_argument("--metrics-output", required=True, type=Path)
    parser.add_argument("--targets", required=True, type=Path, help="Saved Prometheus /api/v1/targets JSON")
    parser.add_argument("--query-results", required=True, type=Path, help="JSON query bundle documented in README.md")
    parser.add_argument("--dashboard", required=True, type=Path, help="Exported Grafana dashboard JSON")
    parser.add_argument("--dashboard-screenshot", required=True, type=Path)
    parser.add_argument("--slo-rule", required=True, type=Path, help="JSON operational rule documented in README.md")
    parser.add_argument("--output-dir", default=Path("submission"), type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root_result = run_git(Path.cwd(), "rev-parse", "--show-toplevel")
    if root_result.returncode != 0:
        print("Run this command from inside the lab-monitoring repository.", file=sys.stderr)
        return 2
    root = Path(root_result.stdout.strip())

    inputs = {
        "Application metrics output": args.metrics_output,
        "Prometheus targets response": args.targets,
        "Prometheus query results": args.query_results,
        "Grafana dashboard export": args.dashboard,
        "Grafana dashboard screenshot": args.dashboard_screenshot,
        "p95 operational rule": args.slo_rule,
    }
    missing = [f"{label}: {path}" for label, path in inputs.items() if not path.is_file()]
    if missing:
        print("Missing required saved evidence:\n  " + "\n  ".join(missing), file=sys.stderr)
        return 2

    head_result = run_git(root, "rev-parse", "HEAD")
    if head_result.returncode != 0:
        print("The repository has no commit to use as an immutable revision.", file=sys.stderr)
        return 2
    commit_sha = head_result.stdout.strip()
    repository_url, repository_valid = sanitize_repository_url(args.repository_url)
    origin_result = run_git(root, "remote", "get-url", "origin")
    origin_url, origin_valid = sanitize_repository_url(origin_result.stdout) if origin_result.returncode == 0 else ("origin-not-found", False)
    repository_matches_origin = repository_valid and origin_valid and repository_url.casefold() == origin_url.casefold()
    tracked = set(run_git(root, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines())
    diff_result = run_git(root, "diff", "--quiet", "HEAD", "--", *REVISION_PATHS)
    immutable = all(path in tracked for path in REVISION_PATHS) and diff_result.returncode == 0 and repository_matches_origin

    source = {path: read_text(root / path) if (root / path).is_file() else "" for path in REVISION_PATHS}
    compose_ok, compose_detail = completed_compose_and_prometheus(
        source["docker-compose.yaml"], source["prometheus/prometheus.yml"]
    )
    metrics_code_ok, metrics_code_detail = completed_metrics_code(source["kafka-monitoring.py"])

    displayed = {label: local_display_path(root, path) for label, path in inputs.items()}
    text_inputs = {
        label: read_text(path) for label, path in inputs.items()
        if label != "Grafana dashboard screenshot"
    }
    metrics_output_ok, metrics_output_detail = valid_metrics_output(text_inputs["Application metrics output"])
    targets_value = parse_object(args.targets)
    targets_ok, target_jobs, targets_detail = valid_targets(targets_value)
    queries_value = parse_object(args.query_results)
    queries_ok, query_panel_ids, query_commit, query_uid, queries_detail = valid_query_results(queries_value)
    dashboard_value = parse_object(args.dashboard)
    dashboard_ok, dashboard_panel_ids, dashboard_uid, dashboard_detail = valid_dashboard(dashboard_value)
    slo_value = parse_object(args.slo_rule)
    slo_ok, slo_uid, slo_panel_id, slo_commit, slo_summary, slo_detail = valid_slo_rule(slo_value)
    screenshot_uri, screenshot_ok = image_data_uri(args.dashboard_screenshot)

    p95_panel_id = dashboard_panel_ids.get("p95_request_latency")
    identifiers_ok = (
        bool(commit_sha and dashboard_uid)
        and query_commit == commit_sha == slo_commit
        and query_uid == dashboard_uid == slo_uid
        and query_panel_ids == dashboard_panel_ids
        and slo_panel_id == p95_panel_id
    )
    identifiers_detail = (
        "The repository commit, dashboard UID, all four panel IDs, and p95 rule reference match across the saved artifacts."
        if identifiers_ok else
        "Use the current commit SHA and exported dashboard UID/panel IDs consistently in query-results.json and slo-rule.json."
    )

    all_text = {**source, **{displayed[label]: content for label, content in text_inputs.items()}}
    withheld = {location for location, content in all_text.items() if contains_secret(content)}
    secrets_ok = not withheld

    checks = [
        check("immutable_repository_revision", immutable, ", ".join(REVISION_PATHS), commit_sha, "The reported GitHub origin matches and all required source is tracked and unchanged at HEAD." if immutable else "Commit the required source files unchanged at HEAD and report the credential-free GitHub origin URL."),
        check("completed_compose_and_prometheus_config", compose_ok, "docker-compose.yaml, prometheus/prometheus.yml", "host gateway + service DNS", compose_detail),
        check("completed_metrics_code", metrics_code_ok, "kafka-monitoring.py", "request_count_total, request_latency_seconds", metrics_code_detail),
        check("application_metrics_output", metrics_output_ok, displayed["Application metrics output"], "request_count_total, request_latency_seconds", metrics_output_detail),
        check("prometheus_targets", targets_ok, displayed["Prometheus targets response"], ", ".join(target_jobs), targets_detail),
        check("saved_query_results", queries_ok, displayed["Prometheus query results"], ", ".join(sorted(query_panel_ids)), queries_detail),
        check("grafana_dashboard", dashboard_ok, displayed["Grafana dashboard export"], dashboard_uid or "dashboard UID missing", dashboard_detail),
        check("matching_artifact_identifiers", identifiers_ok, f"{displayed['Prometheus query results']}, {displayed['Grafana dashboard export']}, {displayed['p95 operational rule']}", dashboard_uid or "dashboard UID missing", identifiers_detail),
        check("p95_operational_rule", slo_ok, displayed["p95 operational rule"], slo_summary, slo_detail),
        check("dashboard_screenshot", screenshot_ok, displayed["Grafana dashboard screenshot"], args.dashboard_screenshot.suffix.casefold(), "A nonempty PNG, JPEG, or WebP dashboard screenshot is available." if screenshot_ok else "Supply a valid PNG, JPEG, or WebP screenshot showing all four panels."),
        check("credential_scan", secrets_ok, ", ".join(sorted(withheld)) if withheld else "all included text", "no obvious plaintext credentials" if secrets_ok else "content withheld", "No common plaintext credential patterns were found." if secrets_ok else "Remove credentials from the named files; their contents were withheld from the report."),
    ]

    evidence = [(path, path, content) for path, content in source.items()]
    evidence.extend(
        (label, displayed[label], content) for label, content in text_inputs.items()
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    report = render_report(
        args.learner, generated_at, repository_url, repository_valid,
        commit_sha, checks, evidence, withheld, screenshot_uri,
    )
    complete = all(item["status"] == "present" for item in checks)
    manifest = {
        "schema_version": 1,
        "checker_version": CHECKER_VERSION,
        "lab": "monitoring",
        "learner": args.learner,
        "generated_at": generated_at,
        "complete": complete,
        "repository": {"url": repository_url, "commit": commit_sha},
        "identifiers": {
            "dashboard_uid": dashboard_uid if displayed["Grafana dashboard export"] not in withheld else "content withheld",
            "panel_ids": dashboard_panel_ids if displayed["Grafana dashboard export"] not in withheld else {},
            "target_jobs": target_jobs if displayed["Prometheus targets response"] not in withheld else [],
        },
        "checks": checks,
        "artifacts": [
            {"name": label, "path": displayed[label], "sha256": sha256(path)}
            for label, path in inputs.items()
        ],
        "manual_spot_checks": ["counter_change", "p95_interpretation"],
        "does_not_contact": ["kafka", "prometheus", "grafana"],
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / REPORT_NAME
    manifest_path = args.output_dir / MANIFEST_NAME
    report_path.write_text(report, encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {report_path} and {manifest_path} ({'complete' if complete else 'incomplete'}).")
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
