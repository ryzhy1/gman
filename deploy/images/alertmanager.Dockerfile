FROM prom/alertmanager:v0.28.1
COPY alerts/alertmanager.yaml alerts/alertmanager.sh /etc/gman-alerts/
# Renders receivers from GMAN_ALERT_* variables, then starts Alertmanager (see the script header).
ENTRYPOINT ["/bin/sh", "/etc/gman-alerts/alertmanager.sh"]
