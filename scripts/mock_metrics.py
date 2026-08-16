"""Expose deterministic application metrics for the environment smoke test."""

import time

from prometheus_client import Counter, Histogram, start_http_server


REQUEST_COUNT = Counter(
    "request_count",
    "Recommendation Request Count",
    ["http_status"],
)
REQUEST_LATENCY = Histogram(
    "request_latency_seconds",
    "Request latency (seconds)",
    buckets=(0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10),
)


def main() -> None:
    start_http_server(8765, addr="0.0.0.0")
    while True:
        REQUEST_COUNT.labels(http_status="200").inc()
        REQUEST_LATENCY.observe(0.05)
        time.sleep(1)


if __name__ == "__main__":
    main()
