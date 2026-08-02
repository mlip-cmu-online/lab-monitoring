from kafka import KafkaConsumer
from prometheus_client import Counter, Histogram, start_http_server

# TODO: Update the Kafka topic to the movie log of your team
# topic = 'movielogN'

start_http_server(8765)

# Metrics like Counter, Gauge, Histogram, Summaries
# Refer https://prometheus.io/docs/concepts/metric_types/ for details of each metric
# This Counter is exposed to Prometheus as request_count_total. The
# http_status label lets dashboards select successful and failed requests.
REQUEST_COUNT = Counter(
    'request_count', 'Recommendation Request Count',
    ['http_status']
)

REQUEST_LATENCY = Histogram(
    'request_latency_seconds', 'Request latency (seconds)',
    buckets=(0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10)
)

def main():
    consumer = KafkaConsumer(
        topic,
        bootstrap_servers='localhost:9092',
        auto_offset_reset='latest',
        group_id=topic,
        enable_auto_commit=True,
        auto_commit_interval_ms=1000
    )

    for message in consumer:
        event = message.value.decode('utf-8')
        values = event.split(',')
        if 'recommendation request' in values[2]:
            # TODO: Increment the request count metric for the appropriate HTTP status code.
            # Hint: Extract the status code from the message and use it as a label for the metric.
            # print(values) - You can print values and see how to get the status
            # status = Eg. 200,400 etc
            # REQUEST_COUNT.labels(http_status=status).inc()

            # Updating request latency histogram
            time_taken = float(values[-1].strip().split(" ")[0])
            REQUEST_LATENCY.observe(time_taken / 1000)

if __name__ == "__main__":
    main()
