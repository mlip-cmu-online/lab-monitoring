# Lab: Monitoring using Prometheus and Grafana

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/mlip-cmu-online/lab-monitoring?quickstart=1)

In this lab, you will gain hands-on experience in monitoring using Prometheus and visualizing metrics through Grafana. Prometheus is an open-source monitoring system for collecting and processing time-series data, while Grafana is a visualization platform for creating interactive dashboards.

You will set up a Prometheus monitoring system, connect it to a Kafka broker, and track key metrics. This practical application will be valuable for your Milestone 3. There are multiple components in this lab, some of which may be new to you, so we strongly advise starting early to familiarize yourself. You can refer to the included screenshot for additional clarity.



## Getting started

### Recommended: GitHub Codespaces

1. Open the course-owned starter with the **Open in GitHub Codespaces** button above and create a Codespace.
   The DevContainer installs Python 3.11, the Python dependencies, SSH tools, and a dedicated Docker daemon with Docker Compose.
2. Wait for the `Lab 8 environment ready` message in the creation log.
3. Before editing any lab files, run the disposable end-to-end environment check:

   ```bash
   bash scripts/smoke_environment.sh
   ```

   The first run downloads the pinned Prometheus, Grafana, and node-exporter images and can take a few minutes.
   The script temporarily exposes synthetic application metrics, starts the Compose stack, verifies ports 3000, 8765, 9090, and 9100, checks every Prometheus target and required PromQL expression, and then cleans up.
   A successful run ends with `Environment smoke test passed`.
4. In the Codespaces **Ports** tab, keep all four forwarded ports **Private**.
   They do not need to be public for this lab.

### Local DevContainer fallback

Clone the [course-owned starter repository](https://github.com/mlip-cmu-online/lab-monitoring), open it in VS Code with the Dev Containers extension, choose **Reopen in Container**, and run the same smoke command.
This path requires Docker on the local computer; Python and the lab dependencies are installed inside the DevContainer.

### Connect the Kafka tunnel

After the smoke test exits, open a separate Codespaces terminal and establish the tunnel in the foreground.
Find `<remote_port>`, `<user>`, and `<remote_server>` in the team project's Canvas instructions.
The local end must be port 9092 because `kafka-monitoring.py` connects to `localhost:9092`:

```bash
ssh -N -L 9092:localhost:<remote_port> <user>@<remote_server>
```

Leave that terminal open while running the monitoring script.
Press **Ctrl+C** in the tunnel terminal when you finish; do not forward port 9092 in the Codespaces **Ports** tab.
If SSH reports that port 9092 is already in use, stop the earlier tunnel before starting another one.

## Background
- Familiarize yourself with the `docker-compose.yaml` and `prometheus.yml` files.
- `docker-compose.yaml` file contains information about all the services inside docker along with their ports. 
- `prometheus.yml` file contains all data that prometheus tracks.

## Setup Docker and Run Kafka Monitoring Script
1. Inspect [docker-compose.yaml](./docker-compose.yaml), especially node exporter's host PID namespace, read-only host-root mount, and `--path.rootfs=/host` setting.
   Determine which host it represents in your environment.
2. Execute `docker compose up -d` in the directory where docker-compose.yaml is located.
3. Check Prometheus Status at http://localhost:9090. Navigate to Status > Targets. Initially, Prometheus will be up, but Kafka-monitoring metrics will be down.
4. Update TODO sections in kafka-monitoring.py to expose relevant metrics to track.
5. Run the Kafka monitoring script: 
    - `python kafka-monitoring.py`

## Prometheus Targets and Queries
1. Verify that all targets are active in Prometheus under Status > Targets.
2. Kafka metrics are visible at http://localhost:8765/metrics, reflecting the metrics you coded in kafka-monitoring.py.
3. Node exporter metrics are available at http://localhost:9100. These are system / host level metrics.
4. Use the Prometheus dashboard's Graph section to explore metrics. Click on the metrics explorer (earth icon) and search for `request_count_total` to visualize total requests with their `http_status` label. Then, try `sum(rate(request_count_total[5m]))` to calculate the request rate across all scraped service instances.
5. Feel free to explore various metrics available in metrics explorer in the table/graph format.

The target addresses deliberately use two different network paths:

* `node-exporter:9100` uses Compose service DNS because Prometheus and node exporter are containers on the same Compose network.
* `host.docker.internal:8765` reaches `kafka-monitoring.py`, which runs as a Python process in the DevContainer rather than as a Compose service.

In Codespaces, Docker-in-Docker makes the DevContainer environment the host of the lab's Docker daemon.
Node exporter therefore describes that hosted lab environment, not your physical laptop and not the individual node-exporter container.
Interpret these values as **Codespace/DevContainer host metrics**.
On the local DevContainer fallback, it likewise describes the DevContainer's Docker environment; it should not be presented as physical-host telemetry.

![prometheus](prometheus.png)

## Configure Grafana Dashboard for Visualization
### Add Data Source in Grafana
1. Access Grafana at http://localhost:3000 and login (default username: admin, password: admin). 
2. Add Prometheus as a data source by navigating to Connections > Data sources > Add source > Prometheus.
3. In the Connection section, set the Prometheus server URL to http://prometheus:9090 (NOT http://localhost:9090).
4. Save and test the data source to confirm connectivity.


### Configure Dashboard
1. Go to Dashboards > create a new dashboard > add visualization.
2. Choose the Prometheus data source you added. Now we will add multiple visualizations (panels) on this dashboard. 
3. You can use `kick start your query` button in Query section at the bottom to explore different query types. You can also click on `code` to write [PromQL](https://grafana.com/blog/2020/02/04/introduction-to-promql-the-prometheus-query-language/) queries.
4. Create panels/visualizations that depict/monitor AT LEAST the 4 following pieces of information:
- Panel 1: Total successful requests - Count the total number of requests with status code 200.
   - Use `sum(request_count_total{http_status="200"})`.
   - Interpret this as the cumulative count exposed by the currently scraped service processes. A process restart resets its counter. To show successful requests observed during the last five minutes instead, use `sum(increase(request_count_total{http_status="200"}[5m]))`.
- Panel 2: Recommendation request rate over time - Show the per-second request rate calculated over a five-minute window.
   - Use `sum(rate(request_count_total[5m]))`. Apply `rate()` before `sum()` so Prometheus can account for counter resets from individual service instances.
- Panel 3: Node CPU usage - Track CPU time consumed in `system` mode by each node.
   - Use `sum by (instance) (rate(node_cpu_seconds_total{mode="system"}[5m]))`.
   - Set the unit to CPU-seconds per second, equivalently the number of CPU cores being used in system mode. This query sums across the CPU cores reported by each node-exporter instance.
- Panel 4: 95th percentile request latency - Track high-end request latency over time across all recommendation-service instances.
   - Use `histogram_quantile(0.95, sum by (le) (rate(request_latency_seconds_bucket[5m])))` and set the unit to seconds.
   - The `le` label identifies each histogram bucket and must remain in the aggregation. To inspect p95 separately by service instance, aggregate with `sum by (instance, le)` instead.

- (Optional) Panel 5: Average Request Latency - Average latency for all recommendation requests
   - HINT: Use forms of `request_latency_seconds`. Make use of formula for average= sum/count

> NOTE: Customize your panels with titles and labels, and adjust the visualizations for better clarity and insight. Please use proper title, axis labels, and axis units to make the visualization clear. Experiment with the Grafana interface to make your dashboard more intuitive. Refer this for [dashboard](https://grafana.com/grafana/dashboards/) configurations.

### Connect p95 Latency to an SLO

Write one operational rule for Panel 4 that includes all three elements below:

1. A numeric p95 latency threshold and the duration for which it must be exceeded before alerting (for example, p95 above 0.5 seconds for 10 minutes).
2. The role that owns the alert, such as the recommendation-service on-call engineer.
3. The first action that role takes, such as checking recent deployments and instance-level latency before deciding whether to roll back or escalate.

Your threshold should express the service objective your team intends to meet; the example values are a format, not a required target.

## FAQ:
1. Q: How to establish an SSH tunnel to the Kafka broker? \
   A: Refer to Lab2 canvas assignment page for more information
2. Q: I cannot log in Grafana using default password. \
   A: If this is not the first time you use Grafana, you possibly have changed the password before. You could reset the password in docker by running command: 'grafana-cli admin reset-admin-password newpassword'.
3. Q: I cannot pass 'save and test' while connecting Prometheus server. \
   A: Make sure you are using the correct URL for Prometheus server. It should be http://prometheus:9090, NOT http://localhost:9090.

## Additional resources
- [PromQL Cheat Sheet](https://promlabs.com/promql-cheat-sheet/)
- [Prometheus Overview](https://prometheus.io/docs/introduction/overview/)
- [Prometheus Latest Documentation](https://prometheus.io/docs/prometheus/latest/migration/)
- [Node Exporter GitHub Repository](https://github.com/prometheus/node_exporter)
- [Alert Manager Overview](https://prometheus.io/docs/alerting/latest/overview/)
- [Grafana Documentation](https://grafana.com/docs/)
---


Note: If you're not seeing any metric data on Prometheus or Grafana, please check if the kafka topic events are not throwing an error. If the topic has events that do not respond back with 200 status code, change the topic to some other topic `movielogN`

