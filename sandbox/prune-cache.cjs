// Registry metadata must not advertise unreviewed versions whose tarballs are
// absent. Otherwise installing a subset (e.g. vitest without a direct vite pin)
// resolves newer transitive dependencies even with npm's offline flag.
const fs = require('node:fs');
const cacache = require('/usr/local/lib/node_modules/npm/node_modules/cacache');
const semver = require('/usr/local/lib/node_modules/npm/node_modules/semver');
const cache = '/opt/npm-cache/_cacache';
const lock = JSON.parse(fs.readFileSync('/opt/toolchain/package-lock.json', 'utf8'));
const allowed = new Map();
function add(name, version) {
  if (!allowed.has(name)) allowed.set(name, new Set());
  allowed.get(name).add(version);
}
for (const [path, item] of Object.entries(lock.packages)) {
  if (path && item.version) add(path.split('node_modules/').at(-1), item.version);
}
add('esbuild', '0.25.9');
add(`@esbuild/linux-${process.arch}`, '0.25.9');
(async () => {
  const entries = await cacache.ls(cache);
  const pruned = new Set();
  for (const [key, entry] of Object.entries(entries)) {
    if (!key.startsWith('make-fetch-happen:request-cache:https://registry.npmjs.org/')) continue;
    const saved = await cacache.get(cache, key);
    let doc;
    try { doc = JSON.parse(saved.data.toString()); } catch { continue; }
    if (!doc.versions || !allowed.has(doc.name)) continue;
    doc.versions = Object.fromEntries(Object.entries(doc.versions)
      .filter(([version]) => allowed.get(doc.name).has(version)));
    const versions = Object.keys(doc.versions);
    if (!versions.length) throw new Error(`No reviewed metadata for ${doc.name}`);
    doc['dist-tags'] = Object.fromEntries(Object.entries(doc['dist-tags'] || {})
      .filter(([, version]) => versions.includes(version)));
    doc['dist-tags'].latest = versions.sort(semver.rcompare)[0];
    if (doc.time) doc.time = Object.fromEntries(Object.entries(doc.time)
      .filter(([version]) => version === 'created' || version === 'modified' || versions.includes(version)));
    await cacache.put(cache, key, JSON.stringify(doc), {metadata: entry.metadata});
    pruned.add(doc.name);
  }
  const missing = [...allowed.keys()].filter(name => !pruned.has(name));
  if (missing.length) throw new Error(`Missing reviewed packuments: ${missing.join(', ')}`);
  // Remove replaced full-registry bodies, reducing the per-worker cache copy too.
  await cacache.verify(cache);
  console.log(`Restricted ${pruned.size} registry documents to reviewed offline versions`);
})().catch(error => { console.error(error); process.exit(1); });
