const fs = require('node:fs');
const path = require('node:path');

const dist = path.join(__dirname, 'dist');
fs.mkdirSync(dist, { recursive: true });
fs.writeFileSync(path.join(dist, 'index.html'),
  '<!doctype html><html><head><title>Demo</title></head><body><h1>Runtime Check Demo</h1></body></html>\n');
console.log('built dist/index.html');
