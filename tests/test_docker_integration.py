"""Real image/engine tests, enabled only in the dedicated Docker CI job."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from agent.docker_sandbox import DockerSandbox, IMAGE
from agent.sandbox_process import SandboxInfrastructureError, SandboxProcessError
from agent.visual_acceptance import verify_visual_acceptance


@unittest.skipUnless(
    os.environ.get("ARCBENCH_RUN_DOCKER_TESTS") == "1",
    "run in the explicit Docker integration job",
)
class DockerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.sandbox = DockerSandbox()

    def project(self, source, script="test"):
        (self.root / "package.json").write_text(
            json.dumps(
                {
                    "name": "sandbox-fixture",
                    "version": "1.0.0",
                    "private": True,
                    "scripts": {script: "node fixture.cjs"},
                }
            )
        )
        (self.root / "fixture.cjs").write_text(source)

    def test_credentials_network_and_git_metadata_are_isolated(self):
        (self.root / ".env").write_text("DO_NOT_COPY=FAKE_HOST_SECRET")
        (self.root / ".git").mkdir()
        (self.root / ".git/config").write_text("trusted-host-metadata")
        outside = self.root.parent / "private.txt"
        outside.write_text("FAKE_OUTSIDE_SECRET")
        self.project(
            """
const fs = require('node:fs'); const os = require('node:os');
if (process.env.OPENAI_API_KEY || process.env.AWS_SECRET_ACCESS_KEY || process.env.NODE_OPTIONS) process.exit(2);
for (const name of ['HTTP_PROXY','HTTPS_PROXY','FTP_PROXY','ALL_PROXY','NO_PROXY'])
  if (process.env[name] || process.env[name.toLowerCase()]) process.exit(5);
if (Object.keys(os.networkInterfaces()).some(name => name !== 'lo')) process.exit(3);
if (fs.existsSync('.env') || fs.existsSync(HOST_PATH)) process.exit(4);
fs.mkdirSync('.git'); fs.writeFileSync('.git/config', 'untrusted');
fs.writeFileSync('audit.json', JSON.stringify({isolated: true, uid: process.getuid()}));
""".replace("HOST_PATH", json.dumps(str(outside))),
            script="build",
        )
        (self.root / "install-check.cjs").write_text(
            "if (process.env.OPENAI_API_KEY || process.env.AWS_SECRET_ACCESS_KEY || "
            "process.env.NODE_OPTIONS) process.exit(9); "
            "require('node:fs').writeFileSync('install-isolated.txt','ok');"
        )
        manifest = json.loads((self.root / "package.json").read_text())
        manifest["dependencies"] = {"react": "19.1.1"}
        manifest["scripts"]["postinstall"] = "node install-check.cjs"
        (self.root / "package.json").write_text(json.dumps(manifest))
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "FAKE_HOST_SECRET",
                "AWS_SECRET_ACCESS_KEY": "FAKE_AWS_SECRET",
                "NODE_OPTIONS": "--require=/host-only-injection.js",
            },
        ):
            sandbox = DockerSandbox()
            endpoint = sandbox._command(
                ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
            )
            self.assertEqual(endpoint.returncode, 0)
            config = Path(self.temp.name) / "docker-config"
            config.mkdir()
            (config / "config.json").write_text(
                json.dumps(
                    {
                        "proxies": {
                            "default": {
                                "httpProxy": "http://fake-user:fake-password@proxy.invalid:8080",
                                "httpsProxy": "http://fake-user:fake-password@proxy.invalid:8080",
                                "ftpProxy": "http://fake-user:fake-password@proxy.invalid:8080",
                                "allProxy": "http://fake-user:fake-password@proxy.invalid:8080",
                                "noProxy": "private-host.invalid",
                            }
                        }
                    }
                )
            )
            sandbox.env.pop("DOCKER_CONTEXT", None)
            sandbox.env["DOCKER_CONFIG"] = str(config)
            sandbox.env["DOCKER_HOST"] = endpoint.stdout.decode().strip()
            result = sandbox.invoke(
                "project_script",
                self.root,
                options={"directory": ".", "script": "build"},
                timeout=60,
            )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((self.root / "install-isolated.txt").read_text(), "ok")
        self.assertNotIn("FAKE_HOST_SECRET", json.dumps(result))
        self.assertNotIn("FAKE_AWS_SECRET", json.dumps(result))
        self.assertEqual(
            json.loads((self.root / "audit.json").read_text()),
            {"isolated": True, "uid": 1000},
        )
        self.assertEqual(
            (self.root / ".git/config").read_text(), "trusted-host-metadata"
        )
        self.assertIn("FAKE_HOST_SECRET", (self.root / ".env").read_text())

    def test_cached_dependencies_install_offline_without_a_project_lockfile(self):
        self.project(
            "const React = require('react'); if (React.version !== '19.1.1') process.exit(7); require('node:fs').writeFileSync('dependency-ok.txt', React.version);"
        )
        package = json.loads((self.root / "package.json").read_text())
        package["dependencies"] = {"react": "19.1.1"}
        (self.root / "package.json").write_text(json.dumps(package))
        result = self.sandbox.invoke(
            "project_script",
            self.root,
            options={"directory": ".", "script": "test"},
            timeout=120,
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((self.root / "dependency-ok.txt").read_text(), "19.1.1")
        self.assertFalse((self.root / "node_modules").exists())

    def test_symlink_export_is_rejected_without_host_mutation(self):
        self.project("require('node:fs').symlinkSync('/etc/passwd','escape');")
        original = (self.root / "fixture.cjs").read_bytes()
        with self.assertRaises(SandboxProcessError):
            self.sandbox.invoke(
                "project_script",
                self.root,
                options={"directory": ".", "script": "test"},
                timeout=60,
            )
        self.assertFalse((self.root / "escape").exists())
        self.assertEqual((self.root / "fixture.cjs").read_bytes(), original)

    def test_vite_vitest_and_native_dependencies_in_real_worker_mounts(self):
        # The former React-only smoke missed esbuild execution and Rollup dlopen.
        for version in ("0.25.9", "0.25.12"):
            with self.subTest(esbuild=version):
                manifest = {"name": "native-cache-regression", "private": True,
                    "type": "module", "scripts": {"build": "vite build", "test": "vitest run"},
                    "dependencies": {"react": "19.1.1", "react-dom": "19.1.1"},
                    "devDependencies": {"vite": "7.1.3", "vitest": "3.2.4", "esbuild": version}}
                (self.root / "package.json").write_text(json.dumps(manifest))
                # Each version must resolve afresh, rather than inheriting another lock.
                (self.root / "package-lock.json").unlink(missing_ok=True)
                (self.root / "index.html").write_text('<div id="root"></div><script type="module" src="/app.jsx"></script>')
                (self.root / "app.jsx").write_text("import React from 'react';import{createRoot}from'react-dom/client';createRoot(document.getElementById('root')).render(<h1>Native dependency check</h1>);")
                (self.root / "native.test.js").write_text("import{it,expect}from'vitest';import{transformSync,version}from'esbuild';it('executes native transform',()=>{expect(version).toBe('" + version + "');expect(transformSync('const x = <h1/>',{loader:'jsx'}).code).toContain('createElement')});")
                result = self.sandbox.invoke("verify_project", self.root, timeout=180)
                checks = result["checks"]
                self.assertTrue(checks and all(item["passed"] for item in checks), checks)
                self.assertFalse((self.root / "node_modules").exists())

    def test_offline_missing_dependency_fails_instead_of_host_fallback(self):
        (self.root / "package.json").write_text(
            json.dumps(
                {
                    "name": "missing-cache-fixture",
                    "version": "1.0.0",
                    "private": True,
                    "dependencies": {"arcbench-not-in-cache-000000": "1.0.0"},
                }
            )
        )
        with self.assertRaises(SandboxInfrastructureError) as failure:
            self.sandbox.invoke("dependencies", self.root, timeout=60)
        self.assertEqual(failure.exception.reason, "dependency_cache_missing")
        self.assertFalse((self.root / "node_modules").exists())

    def test_timeout_leaves_unrelated_container_untouched(self):
        other = "arcbench-control-" + uuid.uuid4().hex
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                other,
                "--network",
                "none",
                "--memory",
                "64m",
                "--user",
                "1000:1000",
                "--entrypoint",
                "python",
                IMAGE,
                "-c",
                "import time; time.sleep(120)",
            ],
            check=True,
            capture_output=True,
        )
        try:
            self.project("setInterval(() => {}, 1000);")
            with self.assertRaises(SandboxProcessError):
                self.sandbox.invoke(
                    "project_script",
                    self.root,
                    options={"directory": ".", "script": "test"},
                    timeout=10,
                )
            self.assertEqual(
                subprocess.run(
                    ["docker", "inspect", other], capture_output=True
                ).returncode,
                0,
            )
            owned = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    "label=arcbench.sandbox.owner",
                    "--format",
                    "{{.ID}}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(owned.stdout.strip(), "")
        finally:
            subprocess.run(
                ["docker", "rm", "-f", other], check=True, capture_output=True
            )

    def test_detached_child_dies_with_owned_container(self):
        self.project("""
const fs = require('node:fs');
const child = require('node:child_process').spawn(process.execPath,
  ['-e', 'setInterval(()=>{},1000)'], {detached: true, stdio: 'ignore'});
fs.writeFileSync('child.json', JSON.stringify({pid: child.pid})); child.unref();
""")
        result = self.sandbox.invoke(
            "project_script",
            self.root,
            options={"directory": ".", "script": "test"},
            timeout=60,
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertGreater(json.loads((self.root / "child.json").read_text())["pid"], 1)
        owned = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=arcbench.sandbox.owner",
                "--format",
                "{{.ID}}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(owned.stdout.strip(), "")

    def test_browser_and_server_share_isolated_loopback_and_model_stays_host_side(self):
        self.project(
            """
const http = require('node:http');
if (process.env.OPENAI_API_KEY || process.env.AWS_SECRET_ACCESS_KEY) process.exit(9);
http.createServer((req,res) => { res.setHeader('Content-Type','text/html');
res.end('<html><body><label for="x">Name</label><input id="x"></body></html>');
}).listen(Number(process.env.PORT), '127.0.0.1');
""",
            script="start",
        )
        manifest = {
            "version": 1,
            "cases": [
                {
                    "name": "form",
                    "reference": "reference.png",
                    "route": "/",
                    "viewport": {"width": 800, "height": 600},
                    "visual_expectations": ["Visible input"],
                }
            ],
            "flows": [
                {
                    "name": "edit",
                    "route": "/",
                    "viewport": {"width": 800, "height": 600},
                    "covers": ["#x"],
                    "steps": [
                        {"action": "fill", "selector": "#x", "value": "hello"},
                        {"action": "expect_value", "selector": "#x", "value": "hello"},
                    ],
                }
            ],
        }
        (self.root / "arcbench-visual-acceptance.json").write_text(json.dumps(manifest))
        calls = []

        def review(**kwargs):
            calls.append(kwargs)
            self.assertTrue(kwargs["screenshot"].startswith(b"\x89PNG\r\n\x1a\n"))
            return {"verdict": "pass", "issues": [], "matched": ["input"]}

        with patch.dict(os.environ, {"OPENAI_API_KEY": "FAKE_VISUAL_SECRET",
                                     "AWS_SECRET_ACCESS_KEY": "FAKE_VISUAL_CLOUD_SECRET"}):
            checked = verify_visual_acceptance(
                self.root,
                self.root,
                ["reference.png"],
                SimpleNamespace(review_visual_case=review),
            )
        self.assertTrue(checked.passed, checked.summary())
        self.assertNotIn("FAKE_VISUAL_SECRET", checked.summary())
        self.assertEqual(len(calls), 1)
        self.assertTrue(
            (self.root / "artifacts/visual-acceptance/report.json").is_file()
        )


if __name__ == "__main__":
    unittest.main()
