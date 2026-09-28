FROM tailscale-grafana:latest

# Replace the default grafana.ini with our OIDC-configured one and the
# provisioning files for the polaris datasource. The base image already
# has `/run.sh` as entrypoint which respects GF_PATHS_* env vars.
COPY grafana.ini /etc/grafana/grafana.ini
COPY provisioning/ /etc/grafana/provisioning/

# The base image runs as grafana user; ensure dirs are writable.
RUN chown -R grafana:grafana /etc/grafana /var/lib/grafana /var/log/grafana 2>/dev/null || true

EXPOSE 3000
CMD ["/run.sh"]