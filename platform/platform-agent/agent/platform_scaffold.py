"""Application-neutral full-stack scaffold: deterministic deployment, zero business logic."""
from __future__ import annotations

import json
from pathlib import Path

DEPLOY = '''#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec > >(tee -a deployment.log) 2>&1
trap 'status=$?; printf "deployment exit=%s stage=%s\\n" "$status" "$stage"' EXIT
stage=dependencies
export npm_config_fetch_retries=0
export npm_config_fetch_timeout=30000
export npm_config_registry=https://registry.npmjs.org
echo "deployment stage=$stage"
if [ -f package-lock.json ]; then
  npm ci --include=dev --no-audit --no-fund
else
  npm install --include=dev --no-audit --no-fund
fi
stage=build
echo "deployment stage=$stage"
npm run build
stage=start
echo "deployment stage=$stage"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-3000}"
exec npm run start
'''

SERVER_INDEX_JS = """import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import express from 'express';
import { initStore } from './store.js';
import { applySeeds } from './seed.js';
import apiRouter from './routes/index.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const distDir = path.resolve(__dirname, '../dist');
const indexHtml = path.join(distDir, 'index.html');

export function createApp() {
  const app = express();
  app.use(express.json({ limit: '5mb' }));

  app.get('/api/health', (_req, res) => {
    res.json({ ok: true });
  });

  if (typeof apiRouter === 'function') {
    app.use('/api', apiRouter);
    app.use(apiRouter);
  }

  app.use('/api', (_req, res) => {
    res.status(404).json({ error: 'Not found' });
  });

  if (fs.existsSync(distDir)) {
    app.use(express.static(distDir));
  }

  app.get('*', (req, res, next) => {
    if (req.path === '/api' || req.path.startsWith('/api/')) {
      return next();
    }
    if (fs.existsSync(indexHtml)) {
      return res.sendFile(indexHtml);
    }
    return res
      .status(200)
      .type('html')
      .send('<!doctype html><html lang="en"><body><div id="root"></div></body></html>');
  });

  return app;
}

export async function startServer() {
  await initStore();
  await applySeeds();
  const app = createApp();
  const host = process.env.HOST || '0.0.0.0';
  const port = Number(process.env.PORT || 3000);
  const server = app.listen(port, host, () => {
    console.log(`Server listening on http://${host}:${port}`);
  });
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.on(signal, () => {
      server.close(() => process.exit(0));
    });
  }
  return server;
}

function samePath(left, right) {
  let a = path.resolve(left);
  let b = path.resolve(right);
  try {
    a = fs.realpathSync(a);
    b = fs.realpathSync(b);
  } catch {
    // Fall back to the resolved paths when one of them cannot be resolved.
  }
  if (process.platform === 'win32') {
    a = a.toLowerCase();
    b = b.toLowerCase();
  }
  return a === b;
}

const isMain = Boolean(process.argv[1]) && samePath(process.argv[1], fileURLToPath(import.meta.url));

if (isMain) {
  startServer().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
"""

SERVER_STORE_JS = """import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

let cachedPath = null;
let state = null;

export function dataDir() {
  return path.resolve(process.env.DATA_DIR || './data');
}

export function storePath() {
  return path.join(dataDir(), 'store.json');
}

function emptyState() {
  return { _meta: {}, collections: {} };
}

function writeAtomic(filePath, content) {
  const dir = path.dirname(filePath);
  fs.mkdirSync(dir, { recursive: true });
  const tmpPath = path.join(
    dir,
    `.store-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}.tmp`
  );
  fs.writeFileSync(tmpPath, content, 'utf8');
  try {
    fs.renameSync(tmpPath, filePath);
  } catch {
    fs.copyFileSync(tmpPath, filePath);
    try {
      fs.unlinkSync(tmpPath);
    } catch {
      // Ignore cleanup errors on locked temporary files.
    }
  }
}

function flush() {
  const target = storePath();
  cachedPath = target;
  writeAtomic(target, JSON.stringify(state, null, 2) + '\\n');
}

function ensureLoaded() {
  const target = storePath();
  if (state && cachedPath === target && fs.existsSync(target)) {
    return state;
  }
  cachedPath = target;
  if (fs.existsSync(target)) {
    const raw = fs.readFileSync(target, 'utf8');
    const parsed = JSON.parse(raw);
    state = {
      _meta: parsed && typeof parsed._meta === 'object' && parsed._meta ? parsed._meta : {},
      collections:
        parsed && typeof parsed.collections === 'object' && parsed.collections
          ? parsed.collections
          : {},
    };
  } else {
    state = emptyState();
    flush();
  }
  return state;
}

export async function initStore() {
  cachedPath = null;
  state = null;
  return ensureLoaded();
}

function collectionList(collection) {
  const current = ensureLoaded();
  if (!Array.isArray(current.collections[collection])) {
    current.collections[collection] = [];
  }
  return current.collections[collection];
}

function clone(value) {
  return structuredClone(value);
}

function matchesPredicate(record, predicate) {
  if (typeof predicate === 'function') {
    return Boolean(predicate(record));
  }
  if (predicate && typeof predicate === 'object') {
    return Object.entries(predicate).every(([key, val]) => record && record[key] === val);
  }
  return true;
}

export function all(collection) {
  return clone(collectionList(collection));
}

export function get(collection, id) {
  const list = collectionList(collection);
  const found = list.find((item) => item && (item.id === id || String(item.id) === String(id)));
  return found ? clone(found) : null;
}

export function find(collection, predicate) {
  const list = collectionList(collection);
  return clone(list.filter((item) => matchesPredicate(item, predicate)));
}

export function findOne(collection, predicate) {
  const list = collectionList(collection);
  const found = list.find((item) => matchesPredicate(item, predicate));
  return found ? clone(found) : null;
}

export function insert(collection, record) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) {
    throw new Error('Record must be a plain object');
  }
  const list = collectionList(collection);
  const entry = clone(record);
  if (entry.id === undefined || entry.id === null || entry.id === '') {
    entry.id = crypto.randomUUID();
  }
  list.push(entry);
  flush();
  return clone(entry);
}

export function update(collection, id, patch) {
  const list = collectionList(collection);
  const index = list.findIndex(
    (item) => item && (item.id === id || String(item.id) === String(id))
  );
  if (index === -1) {
    return null;
  }
  const current = list[index];
  const next =
    typeof patch === 'function'
      ? patch(clone(current))
      : { ...current, ...(patch || {}), id: current.id };
  list[index] = clone(next);
  flush();
  return clone(list[index]);
}

export function remove(collection, id) {
  const list = collectionList(collection);
  const index = list.findIndex(
    (item) => item && (item.id === id || String(item.id) === String(id))
  );
  if (index === -1) {
    return false;
  }
  list.splice(index, 1);
  flush();
  return true;
}

export function getMeta(key) {
  const current = ensureLoaded();
  if (!Object.prototype.hasOwnProperty.call(current._meta, key)) {
    return undefined;
  }
  const value = current._meta[key];
  return value === undefined ? undefined : clone(value);
}

export function setMeta(key, value) {
  const current = ensureLoaded();
  current._meta[key] = clone(value);
  flush();
  return clone(value);
}

export function hashPassword(password) {
  const text = String(password ?? '');
  if (text.startsWith('scrypt$')) {
    const parts = text.split('$');
    if (parts.length === 3 && parts[1] && parts[2]) {
      return text;
    }
  }
  const salt = crypto.randomBytes(16);
  const digest = crypto.scryptSync(text, salt, 64).toString('hex');
  return `scrypt$${salt.toString('hex')}$${digest}`;
}

export function verifyPassword(password, storedHash) {
  if (typeof password !== 'string' || typeof storedHash !== 'string') {
    return false;
  }
  if (!storedHash.startsWith('scrypt$')) {
    return false;
  }
  const parts = storedHash.split('$');
  if (parts.length !== 3 || !parts[1] || !parts[2]) {
    return false;
  }
  try {
    const salt = Buffer.from(parts[1], 'hex');
    const expected = Buffer.from(parts[2], 'hex');
    const actual = crypto.scryptSync(password, salt, expected.length);
    return actual.length === expected.length && crypto.timingSafeEqual(actual, expected);
  } catch {
    return false;
  }
}

export default {
  dataDir,
  storePath,
  initStore,
  all,
  get,
  find,
  findOne,
  insert,
  update,
  remove,
  getMeta,
  setMeta,
  hashPassword,
  verifyPassword,
};
"""

SERVER_SEED_JS = """import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { get, getMeta, hashPassword, insert, setMeta } from './store.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const defaultSeedDir = path.resolve(__dirname, '../seed');

export async function applySeeds(seedDir = defaultSeedDir) {
  if (getMeta('seedsApplied') === true) {
    return { applied: false, files: getMeta('seedFiles') || [] };
  }

  const appliedFiles = [];
  if (fs.existsSync(seedDir)) {
    const entries = fs
      .readdirSync(seedDir)
      .filter((name) => name.toLowerCase().endsWith('.json'))
      .sort();

    for (const filename of entries) {
      const fullPath = path.join(seedDir, filename);
      const raw = fs.readFileSync(fullPath, 'utf8');
      const parsed = JSON.parse(raw);
      if (
        !parsed ||
        typeof parsed !== 'object' ||
        typeof parsed.collection !== 'string' ||
        !parsed.collection.trim() ||
        !Array.isArray(parsed.records)
      ) {
        throw new Error(`Invalid seed file format in ${filename}`);
      }
      const collection = parsed.collection.trim();
      for (const item of parsed.records) {
        if (!item || typeof item !== 'object' || Array.isArray(item)) {
          throw new Error(`Invalid seed record in ${filename}`);
        }
        const record = structuredClone(item);
        if (Object.prototype.hasOwnProperty.call(record, 'password')) {
          if (record.password !== undefined && record.password !== null) {
            record.passwordHash = hashPassword(String(record.password));
          }
          delete record.password;
        }
        if (record.id !== undefined && record.id !== null && record.id !== '') {
          if (get(collection, record.id) !== null) {
            continue;
          }
        }
        insert(collection, record);
      }
      appliedFiles.push(filename);
    }
  }

  setMeta('seedFiles', appliedFiles);
  setMeta('seedsApplied', true);
  return { applied: true, files: appliedFiles };
}

export default applySeeds;
"""

SERVER_ROUTES_INDEX_JS = """import { Router } from 'express';

export const router = Router();

export default router;
"""

SEED_README_MD = """# Seed Data Format

Place prerequisite data files in this directory as `seed/<collection>.json`.
`server/seed.js` loads every `seed/*.json` file in alphabetical order once per fresh data directory.

Each file must be a JSON object with `"collection"` and `"records"`:

```json
{
  "collection": "users",
  "records": [
    {
      "id": "user-1",
      "username": "example_user",
      "password": "ExamplePassword123!"
    }
  ]
}
```

## Rules
- Every record in `"records"` must be a JSON object with a unique `"id"` within the file.
- Any field named `"password"` is automatically replaced with `"passwordHash"` via `hashPassword` (`node:crypto` scrypt) before insertion.
- Only include prerequisite records required to exist before scenario actions begin.
"""

INDEX_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Application</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
"""

VITE_CONFIG_JS = """import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:3000',
    },
  },
  build: {
    outDir: 'dist',
  },
});
"""

SRC_MAIN_JSX = """import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';

const container = document.getElementById('root');
if (container) {
  createRoot(container).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
}
"""

SRC_APP_JSX = """import React from 'react';

export function App() {
  return (
    <main>
      <h1>Application</h1>
    </main>
  );
}

export default App;
"""

SRC_API_JS = """export async function request(path, options = {}) {
  const url =
    path.startsWith('http://') || path.startsWith('https://') || path.startsWith('/api')
      ? path
      : `/api${path.startsWith('/') ? '' : '/'}${path}`;
  const headers = { ...(options.headers || {}) };
  let body = options.body;
  if (
    body !== undefined &&
    body !== null &&
    typeof body === 'object' &&
    !(body instanceof FormData) &&
    !(body instanceof URLSearchParams) &&
    !(body instanceof Blob)
  ) {
    if (!headers['Content-Type'] && !headers['content-type']) {
      headers['Content-Type'] = 'application/json';
    }
    body = JSON.stringify(body);
  }
  const response = await fetch(url, {
    credentials: 'include',
    ...options,
    headers,
    body,
  });
  const contentType = response.headers.get('content-type') || '';
  const data = contentType.includes('application/json')
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const message =
      (data && typeof data === 'object' && (data.error || data.message)) ||
      response.statusText ||
      `Request failed with status ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

export const api = {
  request,
  get: (path, options = {}) => request(path, { ...options, method: 'GET' }),
  post: (path, body, options = {}) => request(path, { ...options, method: 'POST', body }),
  put: (path, body, options = {}) => request(path, { ...options, method: 'PUT', body }),
  patch: (path, body, options = {}) => request(path, { ...options, method: 'PATCH', body }),
  del: (path, options = {}) => request(path, { ...options, method: 'DELETE' }),
};

export default api;
"""

SCAFFOLD_FILES = (
    'package.json',
    'deploy.sh',
    'index.html',
    'vite.config.js',
    'server/index.js',
    'server/store.js',
    'server/seed.js',
    'server/routes/index.js',
    'seed/README.md',
    'src/main.jsx',
    'src/App.jsx',
    'src/api.js',
)

SCAFFOLD_GUIDE = (
    'The project includes a pre-wired full-stack scaffold: '
    'implement backend API endpoints in `server/routes/index.js` (and helper modules under `server/routes/`), '
    'using `server/store.js` (`all`, `get`, `find`, `findOne`, `insert`, `update`, `remove`, `hashPassword`, `verifyPassword`) for durable persistence; '
    'place prerequisite startup data in `seed/<collection>.json` (`{"collection": "...", "records": [...]}` with plaintext `password` fields hashed automatically by `server/seed.js`); '
    'implement the React UI in `src/App.jsx` and `src/` modules using `src/api.js` for `/api` requests. '
    'Do not modify `deploy.sh`, `server/index.js`, `server/store.js`, or `server/seed.js`.'
)


def _package_manifest() -> dict:
    return dict(
        name='arcbench-generated-app',
        version='1.0.0',
        private=True,
        type='module',
        scripts=dict(build='vite build', start='node server/index.js'),
        dependencies={'express': '4.19.2', 'react': '18.3.1', 'react-dom': '18.3.1'},
        devDependencies={
            '@vitejs/plugin-react': '4.3.1',
            'vite': '5.4.0',
            'typescript': '5.4.5',
        },
    )


LAUNCHER_FILES = ('package.json', 'deploy.sh')


def _effectively_empty(project: Path) -> bool:
    """Metadata, dotfiles and README notes do not count as an existing project."""
    if not project.exists():
        return True
    return not any(
        not entry.name.startswith('.') and not entry.name.lower().startswith('readme')
        for entry in project.iterdir()
    )


def prepare_scaffold(project) -> bool:
    """Inject the generic launcher, and the full neutral scaffold when the project is effectively empty.

    A project that already carries a package.json or deploy.sh anywhere keeps
    its own conventions untouched. A project with other substantive files gets
    only the launcher (package.json + deploy.sh), as in the earlier campaign rule.
    """
    project = Path(project)
    if any(project.rglob('package.json')) or any(project.rglob('deploy.sh')):
        return False
    full = _effectively_empty(project)
    project.mkdir(parents=True, exist_ok=True)
    files = {
        'package.json': json.dumps(_package_manifest(), indent=2) + '\n',
        'deploy.sh': DEPLOY,
    }
    if full:
        files.update(_scaffold_sources())
    for rel_path, content in files.items():
        target = project / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8', newline='\n')
    return True


def _scaffold_sources() -> dict:
    return {
        'index.html': INDEX_HTML,
        'vite.config.js': VITE_CONFIG_JS,
        'server/index.js': SERVER_INDEX_JS,
        'server/store.js': SERVER_STORE_JS,
        'server/seed.js': SERVER_SEED_JS,
        'server/routes/index.js': SERVER_ROUTES_INDEX_JS,
        'seed/README.md': SEED_README_MD,
        'src/main.jsx': SRC_MAIN_JSX,
        'src/App.jsx': SRC_APP_JSX,
        'src/api.js': SRC_API_JS,
    }


def is_scaffold(project) -> bool:
    """True when the generic launcher deploy.sh is present byte-for-byte."""
    path = Path(project) / 'deploy.sh'
    return path.is_file() and path.read_text(encoding='utf-8') == DEPLOY


def has_full_scaffold(project) -> bool:
    """True when every generic scaffold file is present (routes, store, seed loader, UI)."""
    project = Path(project)
    return all((project / name).is_file() for name in SCAFFOLD_FILES)


def has_scaffold_seed_loader(project) -> bool:
    """True when server/seed.js is the generic loader whose seed/*.json format validate_seeds checks."""
    path = Path(project) / 'server' / 'seed.js'
    if not path.is_file():
        return False
    try:
        return path.read_text(encoding='utf-8').replace('\r\n', '\n') == SERVER_SEED_JS
    except UnicodeDecodeError:
        return False
