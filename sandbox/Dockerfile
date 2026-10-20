FROM node:24.18.0-bookworm-slim AS node
FROM python:3.12.6-slim-bookworm
COPY --from=node /usr/local/bin/ /usr/local/bin/
COPY --from=node /usr/local/lib/node_modules/ /usr/local/lib/node_modules/
WORKDIR /opt/arcbench
COPY requirements.txt ./
COPY arcbench-agent-runtime ./arcbench-agent-runtime
RUN groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid 1000 --home-dir /tmp/home --no-create-home --shell /usr/sbin/nologin sandbox \
    && pip install --no-cache-dir -r requirements.txt
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN python -m playwright install --with-deps chromium
COPY sandbox/toolchain/package.json sandbox/toolchain/package-lock.json /opt/toolchain/
RUN cd /opt/toolchain && npm ci --ignore-scripts --no-audit --no-fund --cache=/opt/npm-cache
COPY agent ./agent
RUN python -c "from pathlib import Path; from agent.static_names import SCRIPT; Path('/opt/toolchain/check.cjs').write_text(SCRIPT)" \
    && mkdir -p /input /work \
    && chmod -R a+rX /opt/toolchain /opt/npm-cache /ms-playwright
ENV PYTHONPATH=/opt/arcbench
ENV NPM_CONFIG_OFFLINE=true
ENV NPM_CONFIG_CACHE=/tmp/npm-cache
ENV HOME=/tmp/home
USER 1000:1000
ENTRYPOINT ["python", "-m", "agent.sandbox_worker", "/input/request.json"]
