#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $1" >&2
    return 1
  fi
}

for command_name in curl docker jq ssh; do
  require_command "$command_name"
done

if [[ ! -x .venv/bin/python ]]; then
  echo "ERROR: .venv is missing. Rebuild the DevContainer or run the post-create script." >&2
  exit 1
fi

.venv/bin/python -c 'import kafka, prometheus_client'
docker info >/dev/null
docker compose version >/dev/null
docker compose config --quiet

if [[ "${1:-}" == "--preflight" ]]; then
  echo "Preflight passed: Python, Docker Compose, curl, jq, SSH, and the lab dependencies are available."
  exit 0
fi

if [[ -n "$(docker compose ps -q)" ]]; then
  echo "ERROR: the Compose stack is already running. Run this smoke test before beginning the lab." >&2
  exit 1
fi

if curl --silent --fail --max-time 1 http://localhost:8765/metrics >/dev/null 2>&1; then
  echo "ERROR: port 8765 is already in use. Stop kafka-monitoring.py before this smoke test." >&2
  exit 1
fi

mock_pid=""
cleanup() {
  docker compose down --remove-orphans >/dev/null 2>&1 || true
  if [[ -n "$mock_pid" ]]; then
    kill "$mock_pid" >/dev/null 2>&1 || true
    wait "$mock_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

.venv/bin/python scripts/mock_metrics.py &
mock_pid=$!

for attempt in {1..20}; do
  if curl --silent --fail http://localhost:8765/metrics >/dev/null; then
    break
  fi
  if [[ "$attempt" -eq 20 ]]; then
    echo "ERROR: mock application metrics did not start." >&2
    exit 1
  fi
  sleep 1
done

docker compose run --rm --no-deps --entrypoint /bin/promtool prometheus \
  check config /etc/prometheus/prometheus.yml
docker compose up -d

wait_for_url() {
  local name="$1"
  local url="$2"
  for attempt in {1..60}; do
    if curl --silent --fail "$url" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "ERROR: $name did not become ready at $url" >&2
  return 1
}

wait_for_url "Prometheus" "http://localhost:9090/-/ready"
wait_for_url "Grafana" "http://localhost:3000/api/health"
wait_for_url "node exporter" "http://localhost:9100/metrics"

targets_url="http://localhost:9090/api/v1/targets"
for attempt in {1..30}; do
  targets_json="$(curl --silent --fail "$targets_url")"
  if jq -e '
    [.data.activeTargets[] | select(.health == "up") | .labels.job]
    | sort
    | . == ["kafka-monitoring", "node-exporter", "prometheus"]
  ' <<<"$targets_json" >/dev/null; then
    break
  fi
  if [[ "$attempt" -eq 30 ]]; then
    echo "ERROR: not all Prometheus targets became healthy." >&2
    jq -r '.data.activeTargets[] | [.labels.job, .scrapeUrl, .health, .lastError] | @tsv' \
      <<<"$targets_json" >&2
    exit 1
  fi
  sleep 1
done

jq -e '
  any(.data.activeTargets[];
    .labels.job == "kafka-monitoring"
    and (.scrapeUrl | startswith("http://host.docker.internal:8765/")))
  and any(.data.activeTargets[];
    .labels.job == "node-exporter"
    and (.scrapeUrl | startswith("http://node-exporter:9100/")))
' <<<"$targets_json" >/dev/null

# Two scrapes are required before rate() can return a series.
sleep 7

check_query() {
  local title="$1"
  local query="$2"
  local response
  response="$(curl --silent --fail --get \
    --data-urlencode "query=$query" \
    http://localhost:9090/api/v1/query)"
  if ! jq -e '.status == "success" and (.data.result | length > 0)' \
    <<<"$response" >/dev/null; then
    echo "ERROR: PromQL check returned no series: $title" >&2
    echo "Query: $query" >&2
    echo "$response" >&2
    return 1
  fi
  echo "PASS: $title"
}

check_query \
  "total successful requests" \
  'sum(request_count_total{http_status="200"})'
check_query \
  "recommendation request rate" \
  'sum(rate(request_count_total[5m]))'
check_query \
  "node CPU usage" \
  'sum by (instance) (rate(node_cpu_seconds_total{mode="system"}[5m]))'
check_query \
  "p95 request latency" \
  'histogram_quantile(0.95, sum by (le) (rate(request_latency_seconds_bucket[5m])))'

echo "Environment smoke test passed: all ports, targets, network paths, metrics, and required PromQL queries are working."
