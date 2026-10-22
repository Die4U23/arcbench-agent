"""Real process-group cleanup, including a child outliving its npm-like leader."""
import os
import errno
import select
import signal
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

from agent.contract_checks import wait_for_owned_port_release
from agent.sandbox_process import SandboxInfrastructureError


class OwnedPortFailureTests(unittest.TestCase):
    def test_unreleased_port_is_fatal_after_bounded_owned_group_escalation(self):
        kill_signal = getattr(signal, "SIGKILL", 9)
        with patch("agent.contract_checks.socket.socket") as factory, \
             patch("agent.contract_checks.time.monotonic", side_effect=[0, 5, 5, 6]), \
             patch("agent.contract_checks.time.sleep"), \
             patch("agent.contract_checks.os.name", "posix"), \
             patch("agent.contract_checks.os.killpg", create=True) as kill, \
             patch("agent.contract_checks.signal.SIGKILL", kill_signal, create=True):
            factory.return_value.__enter__.return_value.bind.side_effect = OSError(errno.EADDRINUSE, "busy")
            with self.assertRaises(SandboxInfrastructureError) as error:
                wait_for_owned_port_release(4242)
            self.assertEqual(error.exception.reason, "owned_server_cleanup_failed")
            kill.assert_called_once_with(4242, kill_signal)


@unittest.skipIf(os.name == "nt", "Unix process-group regression runs in Linux CI")
class OwnedPortTests(unittest.TestCase):
    def exercise_child(self, ignore_term):
        handler = "lambda *_: None" if ignore_term else "lambda *_: (time.sleep(0.2), sys.exit(0))"
        child = (
            "import socket,signal,time,sys; "
            "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); "
            "s.bind(('127.0.0.1',0)); s.listen(); "
            f"signal.signal(signal.SIGTERM,{handler}); "
            "print(s.getsockname()[1],flush=True); time.sleep(30)"
        )
        leader = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}]).wait()"
        process = subprocess.Popen([sys.executable, "-c", leader], start_new_session=True,
                                   stdout=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], "child did not start")
            port = int(process.stdout.readline())
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
            # This is the old cleanup's return point; the child still owns it.
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with self.assertRaises(OSError):
                    probe.bind(("127.0.0.1", port))
            wait_for_owned_port_release(process.pid, port)
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            process.stdout.close()

    def test_waits_for_child_after_leader_has_exited(self):
        self.exercise_child(False)

    def test_escalates_only_owned_group_when_child_ignores_term(self):
        self.exercise_child(True)
