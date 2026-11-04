const http = require('node:http');

http.createServer((req, res) => res.end('child')).listen(Number(process.env.PORT), '127.0.0.1');
