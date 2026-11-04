const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const port = Number(process.env.PORT || 3000);
const host = process.env.HOST || '0.0.0.0';
const page = fs.readFileSync(path.join(__dirname, 'dist', 'index.html'));

http.createServer((req, res) => {
  res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
  res.end(page);
}).listen(port, host, () => console.log(`listening on ${host}:${port}`));
