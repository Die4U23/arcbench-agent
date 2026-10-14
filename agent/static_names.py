"""Read-only JavaScript name checks independent of generated build scripts."""
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .verify import CheckResult, _truncate

CHECK_NAME = 'frontend JavaScript undefined names'
TIMEOUT = 120
SCRIPT = r'''
const fs = require('node:fs');
const path = require('node:path');
const {Linter} = require('eslint');
const globals = require('globals');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const linter = new Linter();
let failures = 0;
for (const file of payload.files) {
  const name = path.relative(payload.root, file).split(path.sep).join('/');
  const messages = linter.verify(fs.readFileSync(file, 'utf8'), {
    files: ['**/*.{js,jsx,mjs,cjs}'],
    languageOptions: {
      ecmaVersion: 'latest', sourceType: file.endsWith('.cjs') ? 'commonjs' : 'module',
      parserOptions: {ecmaFeatures: {jsx: true}}, globals: globals.browser
    },
    linterOptions: {noInlineConfig: true},
    rules: {'no-undef': 'error'}
  }, {filename: name});
  for (const issue of messages) {
    if (issue.severity < 2) continue;
    failures++;
    console.log(`${name}:${issue.line}:${issue.column}: ${issue.ruleId || 'parse'}: ${issue.message}`);
  }
}
if (!failures) console.log(`Checked ${payload.files.length} JavaScript source files for undefined names; not a behavioral or formal platform pass.`);
process.exitCode = failures ? 1 : 0;
'''


@lru_cache(maxsize=1)
def _tool_directory() -> Path:
    npm = shutil.which('npm') or shutil.which('npm.cmd')
    if not npm:
        raise RuntimeError('npm not found; undefined-name check did not run')
    # Dedicated Agent tooling outside the generated project. Never edit its
    # package manifest, lockfile, source or existing test scripts.
    directory = Path(tempfile.mkdtemp(prefix='arc-agent-undefined-names-'))
    (directory/'package.json').write_text(json.dumps({
        'name': 'arc-agent-undefined-name-check', 'private': True,
        'dependencies': {'eslint': '9.39.5', 'globals': '17.13.0'}}), encoding='utf-8')
    command = [npm, 'install', '--ignore-scripts', '--no-audit', '--no-fund']
    shell = os.name == 'nt' and npm.lower().endswith(('.cmd', '.bat'))
    if shell:
        command = f'"{npm}" install --ignore-scripts --no-audit --no-fund'
    result = subprocess.run(command, shell=shell, cwd=directory,
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=TIMEOUT, check=False)
    if result.returncode:
        raise RuntimeError('Undefined-name tooling installation failed:\n' +
                           _truncate(result.stdout + result.stderr))
    (directory/'check.cjs').write_text(SCRIPT, encoding='utf-8')
    return directory


def verify_frontend_names(project_dir: Path) -> CheckResult | None:
    root = project_dir.resolve()
    source = root/'frontend/src'
    if not source.is_dir():
        return None
    files = []
    for current, dirs, names in os.walk(source, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in {
            'node_modules', 'dist', 'build', 'test', 'tests', '__tests__'}
            and not d.startswith('.') and not (Path(current)/d).is_symlink())
        for name in sorted(names):
            path = Path(current)/name
            if path.suffix not in {'.js', '.jsx', '.mjs', '.cjs'} or any(
                token in name for token in ('.test.', '.spec.')):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                return CheckResult(CHECK_NAME, False, 1, 'Linked frontend source cannot be checked')
            files.append(str(path))
    if not files:
        return None
    node = shutil.which('node')
    if not node:
        return CheckResult(CHECK_NAME, False, None, 'node not found; name check did not run')
    try:
        directory = _tool_directory()
        result = subprocess.run([node, str(directory/'check.cjs')], cwd=directory,
            input=json.dumps({'root': str(root), 'files': files}), capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=TIMEOUT, check=False)
        return CheckResult(CHECK_NAME, result.returncode == 0, result.returncode,
                           _truncate(result.stdout + result.stderr))
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return CheckResult(CHECK_NAME, False, None,
                           'Undefined-name check unavailable (not a pass): ' + str(exc))
