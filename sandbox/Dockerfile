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
# npm ci caches locked tarballs but fresh generated projects also need registry
# registry metadata for offline resolution. npm CLI adds /_cacache to its cache
# root, whereas pacote expects the content-cache directory directly. Use the same
# store and full metadata request as npm install. Seed reviewed versions without
# running any package scripts or downloading every foreign-platform tarball.
RUN cd /opt/toolchain && node -e "const p=require('/usr/local/lib/node_modules/npm/node_modules/pacote'); const lock=require('./package-lock.json'); (async()=>{ for (const [path,item] of Object.entries(lock.packages)) { if (!path || !item.version || !item.resolved) continue; const name=path.split('node_modules/').at(-1); await p.manifest(name+'@'+item.version,{cache:'/opt/npm-cache/_cacache',registry:'https://registry.npmjs.org/',fullMetadata:true,ignoreScripts:true}); } })().catch(e=>{console.error(e);process.exit(1)});"
# A no-lock install validates that current cached metadata resolves to cached
# tarballs. Cache drift fails the image build instead of becoming a runtime trap.
COPY sandbox/prune-cache.cjs /opt/toolchain/prune-cache.cjs
RUN node /opt/toolchain/prune-cache.cjs
RUN mkdir /tmp/cache-smoke && cp /opt/toolchain/package.json /tmp/cache-smoke/ \
    && cd /tmp/cache-smoke && npm install --offline --ignore-scripts --no-audit --no-fund --cache=/opt/npm-cache \
    && rm -rf /tmp/cache-smoke && du -sh /opt/npm-cache
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
