import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from agent.docker_sandbox import DockerSandbox, _encode
from agent.remote_sandbox import RemoteSandbox
from agent.sandbox_process import SandboxProcessError
from scripts.sandbox_gateway import Gateway, BoundedServer, handler


class RemoteSandboxTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.token = "test-only-" + "x" * 48
        self.image = "sha256:" + "1" * 64
        self.gateway = Gateway(self.token, time.time()+60, self.image, executor=self.execute)
        self.server = BoundedServer(("127.0.0.1", 0), handler(self.gateway))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)
        self.config = dict(endpoint="http://127.0.0.1:"+str(self.server.server_port),
                           token=self.token, image=self.image, expires=time.time()+60)

    def cleanup(self):
        self.server.shutdown(); self.server.server_close()
        self.gateway.pool.shutdown(wait=True)

    def execute(self, payload):
        self.calls.append(payload)
        return {"state":"done", "image":self.image, "result":{"exit_code":0},
                "files":_encode({"source.js":b"new"})}

    def test_roundtrip_preserves_credentials_and_reconciles_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/"source.js").write_text("old")
            (root/".env").write_text("SECRET")
            client = RemoteSandbox(self.config)
            client.check_available()
            self.assertEqual(client.invoke("project_script",root,options={"directory":".","script":"test"}),{"exit_code":0})
            self.assertEqual((root/"source.js").read_text(),"new")
            self.assertEqual((root/".env").read_text(),"SECRET")
            self.assertNotIn("SECRET",json.dumps(self.calls))
            self.assertNotIn(self.token,json.dumps(self.calls))

    def test_unauthenticated_request_never_executes_or_reports_image(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.config["endpoint"]+"/health")
        self.assertEqual(error.exception.code,403)
        self.assertNotIn(self.image,error.exception.read().decode())
        self.assertEqual(self.calls,[])

    def test_expiry_and_image_mismatch_fail_closed(self):
        client = RemoteSandbox(self.config)
        self.gateway.expires = time.time()-1
        with self.assertRaises(SandboxProcessError): client.check_available()
        self.gateway.expires = time.time()+60
        client.image = "sha256:different"
        with self.assertRaisesRegex(SandboxProcessError,"mismatch"): client.check_available()

    def test_invalid_paths_commands_and_duplicates_cannot_trigger_extra_execution(self):
        payload=dict(operation="project_script",options={},timeout=1,files={},task={})
        bad=dict(payload,files={"../escape":"eA=="})
        with self.assertRaises(ValueError): self.gateway.submit("bad",json.dumps(bad).encode())
        with self.assertRaises(ValueError): self.gateway.submit("bad",json.dumps(dict(payload,operation="shell")).encode())
        raw=json.dumps(payload).encode()
        self.gateway.submit("first",raw)
        self.gateway.jobs["first"][1].result()
        self.gateway.submit("first",raw)
        self.gateway.submit("second",raw)
        self.gateway.jobs["second"][1].result()
        self.gateway.submit("first",raw)
        self.assertEqual(self.gateway.result("first"),{"state":"expired"})
        self.assertEqual(len(self.calls),2)
        with self.assertRaises(ValueError): self.gateway.submit("first",raw+b" ")

    def test_controller_changes_while_waiting_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/"source.js").write_text("old")
            def edit(payload):
                (root/"source.js").write_text("user edit")
                return self.execute(payload)
            self.gateway.executor=edit
            with self.assertRaises(ValueError): RemoteSandbox(self.config).invoke("dependencies",root)
            self.assertEqual((root/"source.js").read_text(),"user edit")

    def test_remote_failure_never_falls_back_to_host_or_local_docker(self):
        with patch('agent.remote_sandbox.configured_remote', return_value=RemoteSandbox(self.config)), patch('agent.docker_sandbox.shutil.which') as which:
            self.gateway.expires=time.time()-1
            with self.assertRaises(SandboxProcessError): DockerSandbox().check_available()
            which.assert_not_called()


@unittest.skipUnless(os.environ.get('ARCBENCH_RUN_DOCKER_TESTS')=='1','real Docker integration')
class RemoteDockerIntegrationTests(unittest.TestCase):
    def test_http_transport_keeps_real_docker_boundary(self):
        sandbox=DockerSandbox()
        image=sandbox._command(['image','inspect','--format','{{.Id}}','arcbench-sandbox:1']).stdout.decode().strip()
        token='test-only-'+'z'*48
        gateway=Gateway(token,time.time()+180,image)
        server=BoundedServer(('127.0.0.1',0),handler(gateway))
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder)
                (root/'package.json').write_text(json.dumps({'name':'remote-check','private':True,'scripts':{'test':'node check.cjs'}}))
                (root/'check.cjs').write_text("const fs=require('fs');const assert=require('assert');assert.equal(process.getuid(),1000);assert.ok(!process.env.OPENAI_API_KEY);assert.ok(!fs.existsSync('/var/run/docker.sock'));assert.deepEqual(Object.keys(require('os').networkInterfaces()),['lo']);fs.writeFileSync('done.txt','isolated');")
                client=RemoteSandbox(dict(endpoint=f'http://127.0.0.1:{server.server_port}',token=token,image=image,expires=time.time()+180))
                with patch.dict(os.environ,{'OPENAI_API_KEY':'SENTINEL_NEVER_EXPORT'}):
                    result=client.invoke('project_script',root,options={'directory':'.','script':'test'},timeout=60)
                self.assertEqual(result['exit_code'],0,result)
                self.assertEqual((root/'done.txt').read_text(),'isolated')
            # Reuse the established real Chromium/server contract over HTTP;
            # the visual reviewer still runs only in the controller.
            from tests.test_docker_integration import DockerIntegrationTests
            case=DockerIntegrationTests('test_browser_and_server_share_isolated_loopback_and_model_stays_host_side')
            case.setUp()
            try:
                with patch('agent.container_visual.DockerSandbox',return_value=client):
                    case.test_browser_and_server_share_isolated_loopback_and_model_stays_host_side()
            finally:
                case.doCleanups()
        finally:
            server.shutdown();server.server_close();gateway.pool.shutdown(wait=True)


if __name__ == '__main__': unittest.main()
