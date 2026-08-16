#!/usr/bin/env python3
"""Save live Lab 8 Prometheus evidence for later offline report generation."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen


REQUIRED_QUERIES = {
    "total_successful_requests": 'sum(request_count_total{http_status="200"})',
    "request_rate": "sum(rate(request_count_total[5m]))",
    "node_cpu_usage": 'sum by (instance) (rate(node_cpu_seconds_total{mode="system"}[5m]))',
    "p95_request_latency": "histogram_quantile(0.95, sum by (le) (rate(request_latency_seconds_bucket[5m])))",
}


def normalize_query(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", "", value).replace("'", '"').casefold()


def dashboard_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
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
        if isinstance(panel.get("panels"), list):
            flat.extend(flatten_panels(panel["panels"]))
        if isinstance(panel.get("targets"), list):
            flat.append(panel)
    return flat


def required_panel_ids(dashboard: dict[str, Any]) -> tuple[str, dict[str, int]]:
    uid = str(dashboard.get("uid", "")).strip()
    matches: dict[str, int] = {}
    for panel in flatten_panels(dashboard.get("panels")):
        panel_id = panel.get("id")
        if not isinstance(panel_id, int) or isinstance(panel_id, bool):
            continue
        expressions = {
            normalize_query(target.get("expr"))
            for target in panel.get("targets", []) if isinstance(target, dict)
        }
        for name, query in REQUIRED_QUERIES.items():
            if normalize_query(query) in expressions:
                matches[name] = panel_id
    return uid, matches


def fetch_text(url: str) -> str:
    with urlopen(url, timeout=15) as response:  # noqa: S310 - learner-selected local endpoint
        return response.read().decode("utf-8", errors="replace")


def fetch_json(url: str) -> dict[str, Any]:
    value = json.loads(fetch_text(url))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object from {url}")
    return value


def current_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        raise ValueError("Run this command inside a committed lab-monitoring repository.")
    return result.stdout.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture metrics, targets, and the four required query results while the monitoring stack is running."
    )
    parser.add_argument("--dashboard", required=True, type=Path, help="Grafana dashboard JSON exported after creating all panels")
    parser.add_argument("--output-dir", default=Path("evidence"), type=Path)
    parser.add_argument("--metrics-url", default="http://localhost:8765/metrics")
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        dashboard_value = json.loads(args.dashboard.read_text(encoding="utf-8"))
        dashboard = dashboard_object(dashboard_value)
        if dashboard is None:
            raise ValueError("The dashboard export is not a JSON object.")
        dashboard_uid, panel_ids = required_panel_ids(dashboard)
        missing_panels = sorted(set(REQUIRED_QUERIES) - set(panel_ids))
        if not dashboard_uid or missing_panels:
            raise ValueError(
                "Save the dashboard with a UID and the four exact required queries before capture. "
                f"Missing query panels: {', '.join(missing_panels) or 'none'}."
            )
        commit = current_commit()
        metrics_text = fetch_text(args.metrics_url)
        prometheus_base = args.prometheus_url.rstrip("/")
        targets = fetch_json(f"{prometheus_base}/api/v1/targets")
        queries = []
        for name, query in REQUIRED_QUERIES.items():
            response = fetch_json(f"{prometheus_base}/api/v1/query?{urlencode({'query': query})}")
            result = response.get("data", {}).get("result", []) if isinstance(response.get("data"), dict) else []
            if response.get("status") != "success" or not isinstance(result, list) or not result:
                raise ValueError(f"The saved result for {name} is empty or unsuccessful.")
            queries.append(
                {
                    "name": name,
                    "panel_id": panel_ids[name],
                    "query": query,
                    "response": response,
                }
            )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Evidence capture failed: {error}", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "metrics.txt").write_text(metrics_text, encoding="utf-8")
    (args.output_dir / "targets.json").write_text(
        json.dumps(targets, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "query-results.json").write_text(
        json.dumps(
            {
                "repository_commit": commit,
                "dashboard_uid": dashboard_uid,
                "queries": queries,
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"Saved metrics.txt, targets.json, and query-results.json in {args.output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
