# Both Collector roles of the GMan server: the gateway (default) and the node collector of this host
# (compose service `node-otel`, --config=/etc/otelcol/node.yaml).
FROM otel/opentelemetry-collector-contrib:0.162.0
COPY otel-gateway.yaml /etc/otelcol/config.yaml
COPY otel-gateway.prod.yaml /etc/otelcol/gateway.prod.yaml
COPY otel-node.yaml /etc/otelcol/node.yaml
COPY otel-node-gpu.yaml /etc/otelcol/node-gpu.yaml
CMD ["--config=/etc/otelcol/config.yaml"]
