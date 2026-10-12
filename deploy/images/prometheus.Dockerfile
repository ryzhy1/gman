FROM prom/prometheus:v3.5.0
COPY prometheus.yaml /etc/prometheus/prometheus.yml
COPY alerts/rules.yaml /etc/prometheus/rules/gman.yaml
