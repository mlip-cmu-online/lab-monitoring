import json
import os
import subprocess
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


SCRIPT = Path(__file__).parents[1] / "scripts" / "generate-monitoring-submission.py"
CAPTURE_SCRIPT = Path(__file__).parents[1] / "scripts" / "capture-monitoring-evidence.py"

QUERIES = {
    "total_successful_requests": 'sum(request_count_total{http_status="200"})',
    "request_rate": "sum(rate(request_count_total[5m]))",
    "node_cpu_usage": 'sum by (instance) (rate(node_cpu_seconds_total{mode="system"}[5m]))',
    "p95_request_latency": "histogram_quantile(0.95, sum by (le) (rate(request_latency_seconds_bucket[5m])))",
}


def run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, env=env, check=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


class GenerateMonitoringSubmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        run("git", "init", "-b", "main", cwd=self.root)
        run("git", "config", "user.name", "Test Learner", cwd=self.root)
        run("git", "config", "user.email", "learner@example.com", cwd=self.root)
        run(
            "git", "remote", "add", "origin",
            "https://github.com/test-learner/lab-monitoring.git", cwd=self.root,
        )
        self._write_revision()
        run("git", "add", ".", cwd=self.root)
        run("git", "commit", "-m", "complete monitoring lab", cwd=self.root)
        self.commit = run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip()
        self._write_evidence()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

    def _write_revision(self) -> None:
        self._write(
            "docker-compose.yaml",
            """
            services:
              prometheus:
                image: prom/prometheus:v3.5.0
                extra_hosts:
                  - 'host.docker.internal:host-gateway'
                ports:
                  - '9090:9090'
              node-exporter:
                image: quay.io/prometheus/node-exporter:v1.9.1
                pid: host
                command:
                  - '--path.rootfs=/host'
                volumes:
                  - '/:/host:ro'
                ports:
                  - '9100:9100'
              grafana:
                image: grafana/grafana-oss:12.1.0
                ports:
                  - '3000:3000'
            """,
        )
        self._write(
            "prometheus/prometheus.yml",
            """
            scrape_configs:
              - job_name: 'prometheus'
                static_configs:
                  - targets: ['localhost:9090']
              - job_name: 'kafka-monitoring'
                static_configs:
                  - targets: ['host.docker.internal:8765']
              - job_name: 'node-exporter'
                static_configs:
                  - targets: ['node-exporter:9100']
            """,
        )
        self._write(
            "kafka-monitoring.py",
            """
            from prometheus_client import Counter, Histogram

            topic = "movielog7"
            REQUEST_COUNT = Counter(
                "request_count", "Recommendation Request Count", ["http_status"]
            )
            REQUEST_LATENCY = Histogram(
                "request_latency_seconds", "Request latency (seconds)"
            )

            status = "200"
            REQUEST_COUNT.labels(http_status=status).inc()
            REQUEST_LATENCY.observe(0.12)
            """,
        )

    def _write_evidence(self) -> None:
        self._write(
            "evidence/metrics.txt",
            """
            # TYPE request_count_total counter
            request_count_total{http_status="200"} 12.0
            # TYPE request_latency_seconds histogram
            request_latency_seconds_bucket{le="0.5"} 11.0
            request_latency_seconds_count 12.0
            request_latency_seconds_sum 1.44
            """,
        )
        targets = {
            "status": "success",
            "data": {
                "activeTargets": [
                    {"labels": {"job": "prometheus"}, "scrapeUrl": "http://localhost:9090/metrics", "health": "up"},
                    {"labels": {"job": "kafka-monitoring"}, "scrapeUrl": "http://host.docker.internal:8765/metrics", "health": "up"},
                    {"labels": {"job": "node-exporter"}, "scrapeUrl": "http://node-exporter:9100/metrics", "health": "up"},
                ]
            },
        }
        self._write("evidence/targets.json", json.dumps(targets, indent=2))
        query_records = []
        for panel_id, (name, query) in enumerate(QUERIES.items(), 1):
            query_records.append(
                {
                    "name": name,
                    "panel_id": panel_id,
                    "query": query,
                    "response": {
                        "status": "success",
                        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, "1"]}]},
                    },
                }
            )
        self._write(
            "evidence/query-results.json",
            json.dumps(
                {
                    "repository_commit": self.commit,
                    "dashboard_uid": "recommendation-health",
                    "queries": query_records,
                },
                indent=2,
            ),
        )
        panels = []
        for panel_id, (name, query) in enumerate(QUERIES.items(), 1):
            panels.append(
                {"id": panel_id, "title": name.replace("_", " ").title(), "targets": [{"expr": query}]}
            )
        self._write(
            "evidence/dashboard.json",
            json.dumps({"uid": "recommendation-health", "title": "Recommendation health", "panels": panels}, indent=2),
        )
        self._write(
            "evidence/slo-rule.json",
            json.dumps(
                {
                    "repository_commit": self.commit,
                    "dashboard_uid": "recommendation-health",
                    "panel_id": 4,
                    "threshold_seconds": 0.5,
                    "duration": "10m",
                    "owner": "recommendation-service on-call engineer",
                    "first_action": "Compare instance latency and check the latest deployment.",
                },
                indent=2,
            ),
        )
        (self.root / "evidence/dashboard.png").write_bytes(b"\x89PNG\r\n\x1a\nmonitoring-dashboard")

    def generate(self, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return run(
            "python3", str(SCRIPT),
            "--learner", "Test Learner",
            "--repository-url", "https://github.com/test-learner/lab-monitoring",
            "--metrics-output", "evidence/metrics.txt",
            "--targets", "evidence/targets.json",
            "--query-results", "evidence/query-results.json",
            "--dashboard", "evidence/dashboard.json",
            "--dashboard-screenshot", "evidence/dashboard.png",
            "--slo-rule", "evidence/slo-rule.json",
            cwd=self.root, env=env,
        )

    def manifest(self) -> dict:
        return json.loads(
            (self.root / "submission/monitoring-manifest.json").read_text(encoding="utf-8")
        )

    def test_generates_complete_self_contained_report_without_live_services(self) -> None:
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        markers = []
        for executable_name in ("docker", "curl", "promtool", "grafana"):
            marker = self.root / f"{executable_name}-was-called"
            executable = fake_bin / executable_name
            executable.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 99\n", encoding="utf-8")
            executable.chmod(0o755)
            markers.append(marker)
        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}:{env['PATH']}"

        result = self.generate(env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(marker.exists() for marker in markers))
        manifest = self.manifest()
        report = (self.root / "submission/monitoring-report.html").read_text(encoding="utf-8")
        self.assertTrue(manifest["complete"])
        self.assertTrue(all(item["status"] == "present" for item in manifest["checks"]))
        self.assertEqual(manifest["identifiers"]["dashboard_uid"], "recommendation-health")
        self.assertEqual(manifest["manual_spot_checks"], ["counter_change", "p95_interpretation"])
        self.assertIn("data:image/png;base64,", report)
        self.assertIn("request_count_total", report)

    def test_marks_dirty_revision_and_mismatched_identifiers_incomplete(self) -> None:
        compose = self.root / "docker-compose.yaml"
        compose.write_text(compose.read_text(encoding="utf-8") + "\n# uncommitted\n", encoding="utf-8")
        slo = json.loads((self.root / "evidence/slo-rule.json").read_text(encoding="utf-8"))
        slo["dashboard_uid"] = "different-dashboard"
        self._write("evidence/slo-rule.json", json.dumps(slo, indent=2))

        result = self.generate()

        self.assertEqual(result.returncode, 1)
        statuses = {item["name"]: item["status"] for item in self.manifest()["checks"]}
        self.assertEqual(statuses["immutable_repository_revision"], "missing")
        self.assertEqual(statuses["matching_artifact_identifiers"], "missing")

    def test_rejects_missing_dashboard_panel_and_empty_query_result(self) -> None:
        dashboard = json.loads((self.root / "evidence/dashboard.json").read_text(encoding="utf-8"))
        dashboard["panels"] = dashboard["panels"][:-1]
        self._write("evidence/dashboard.json", json.dumps(dashboard, indent=2))
        query_results = json.loads((self.root / "evidence/query-results.json").read_text(encoding="utf-8"))
        query_results["queries"][1]["response"]["data"]["result"] = []
        self._write("evidence/query-results.json", json.dumps(query_results, indent=2))

        result = self.generate()

        self.assertEqual(result.returncode, 1)
        statuses = {item["name"]: item["status"] for item in self.manifest()["checks"]}
        self.assertEqual(statuses["saved_query_results"], "missing")
        self.assertEqual(statuses["grafana_dashboard"], "missing")

    def test_rejects_unfinished_metric_code(self) -> None:
        source = self.root / "kafka-monitoring.py"
        source.write_text(
            source.read_text(encoding="utf-8").replace(
                "REQUEST_COUNT.labels(http_status=status).inc()",
                "# TODO: increment request count",
            ),
            encoding="utf-8",
        )
        run("git", "commit", "-am", "leave metric placeholder", cwd=self.root)
        new_commit = run("git", "rev-parse", "HEAD", cwd=self.root).stdout.strip()
        for evidence_name in ("query-results.json", "slo-rule.json"):
            value = json.loads((self.root / f"evidence/{evidence_name}").read_text(encoding="utf-8"))
            value["repository_commit"] = new_commit
            self._write(f"evidence/{evidence_name}", json.dumps(value, indent=2))

        result = self.generate()

        self.assertEqual(result.returncode, 1)
        statuses = {item["name"]: item["status"] for item in self.manifest()["checks"]}
        self.assertEqual(statuses["immutable_repository_revision"], "present")
        self.assertEqual(statuses["completed_metrics_code"], "missing")

    def test_withholds_plaintext_credentials_from_outputs(self) -> None:
        secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
        targets = json.loads((self.root / "evidence/targets.json").read_text(encoding="utf-8"))
        targets["api_key"] = secret
        self._write("evidence/targets.json", json.dumps(targets, indent=2))

        result = self.generate()

        self.assertEqual(result.returncode, 1)
        combined = (
            (self.root / "submission/monitoring-report.html").read_text(encoding="utf-8")
            + (self.root / "submission/monitoring-manifest.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(secret, combined)
        self.assertIn("Content withheld", combined)
        self.assertEqual(self.manifest()["identifiers"]["target_jobs"], [])

    def test_rejects_missing_evidence_without_creating_outputs(self) -> None:
        (self.root / "evidence/dashboard.png").unlink()

        result = self.generate()

        self.assertEqual(result.returncode, 2)
        self.assertIn("Missing required saved evidence", result.stderr)
        self.assertFalse((self.root / "submission").exists())

    def test_capture_helper_saves_live_evidence_for_offline_generation(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler_self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                parsed = urlsplit(handler_self.path)
                if parsed.path == "/metrics":
                    body = (self.root / "evidence/metrics.txt").read_bytes()
                    content_type = "text/plain"
                elif parsed.path == "/api/v1/targets":
                    body = (self.root / "evidence/targets.json").read_bytes()
                    content_type = "application/json"
                elif parsed.path == "/api/v1/query" and parse_qs(parsed.query).get("query"):
                    body = json.dumps(
                        {
                            "status": "success",
                            "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, "1"]}]},
                        }
                    ).encode()
                    content_type = "application/json"
                else:
                    handler_self.send_error(404)
                    return
                handler_self.send_response(200)
                handler_self.send_header("Content-Type", content_type)
                handler_self.send_header("Content-Length", str(len(body)))
                handler_self.end_headers()
                handler_self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        capture_dir = self.root / "captured"
        try:
            result = run(
                "python3", str(CAPTURE_SCRIPT),
                "--dashboard", "evidence/dashboard.json",
                "--output-dir", str(capture_dir),
                "--metrics-url", f"http://127.0.0.1:{port}/metrics",
                "--prometheus-url", f"http://127.0.0.1:{port}",
                cwd=self.root,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = json.loads((capture_dir / "query-results.json").read_text(encoding="utf-8"))
        self.assertEqual(bundle["repository_commit"], self.commit)
        self.assertEqual(bundle["dashboard_uid"], "recommendation-health")
        self.assertEqual(len(bundle["queries"]), 4)
        self.assertTrue((capture_dir / "metrics.txt").is_file())
        self.assertTrue((capture_dir / "targets.json").is_file())


if __name__ == "__main__":
    unittest.main()
