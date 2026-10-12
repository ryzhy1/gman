FROM prom/prometheus:v3.5.0
COPY prometheus.yaml /etc/prometheus/prometheus.template.yml
COPY alerts/rules.yaml /etc/prometheus/rules/gman.yaml
# The scrape and evaluation interval comes from GMAN_METRICS_INTERVAL (seconds, default 5); the rendered config is
# written to the data volume, the only writable path. Compose passes --config.file=/prometheus/prometheus.yml.
ENTRYPOINT ["/bin/sh", "-c", "sed \"s/__METRICS_INTERVAL__/${GMAN_METRICS_INTERVAL:-5}s/g\" /etc/prometheus/prometheus.template.yml > /prometheus/prometheus.yml && exec /bin/prometheus \"$@\"", "--"]
