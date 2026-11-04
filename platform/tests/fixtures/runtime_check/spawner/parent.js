const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const child = spawn(process.execPath, [path.join(__dirname, 'child.js')], { stdio: 'inherit', env: process.env });
fs.writeFileSync(process.env.PID_FILE, JSON.stringify({ parent: process.pid, child: child.pid }));
setInterval(() => {}, 1000);
