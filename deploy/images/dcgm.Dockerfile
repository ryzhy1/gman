# NVIDIA DCGM Exporter with the GMan counter set (deploy/dcgm-counters.csv): GPU metrics for charts, dashboards and
# alert rules. Service `dcgm-exporter` of deploy/compose.nvidia.yaml and deploy/host/compose.nvidia.yaml.
FROM nvcr.io/nvidia/k8s/dcgm-exporter:4.5.2-4.8.1-ubuntu22.04
COPY dcgm-counters.csv /etc/dcgm-exporter/gman.csv
ENV DCGM_EXPORTER_COLLECTORS=/etc/dcgm-exporter/gman.csv
# Collection interval in ms from GMAN_METRICS_INTERVAL (seconds, default 5) unless DCGM_EXPORTER_INTERVAL is set.
ENTRYPOINT ["/bin/sh", "-c", "export DCGM_EXPORTER_INTERVAL=${DCGM_EXPORTER_INTERVAL:-$((${GMAN_METRICS_INTERVAL:-5} * 1000))}; exec /usr/local/dcgm/dcgm-exporter-entrypoint.sh \"$@\"", "--"]
