import multiprocessing
import os
import tempfile
import unittest

from runtime_lock import WorkflowBusyError, WorkflowLease


def _hold_lease(workspace, runtime_dir, ready):
    with WorkflowLease(
        workspace,
        runtime_dir=runtime_dir,
        run_id="child-run",
        platform="Chaturbate",
        task="synthetic",
    ):
        ready.send(True)
        ready.close()
        multiprocessing.Event().wait(30)


class RuntimeLockTests(unittest.TestCase):
    def test_cross_process_lease_rejects_second_writer_and_releases_on_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = os.path.join(tmp, "workspace")
            runtime_dir = os.path.join(tmp, "runtime")
            os.makedirs(workspace)
            receive, send = multiprocessing.Pipe(duplex=False)
            process = multiprocessing.Process(
                target=_hold_lease,
                args=(workspace, runtime_dir, send),
            )
            process.start()
            self.assertTrue(receive.poll(5))
            self.assertTrue(receive.recv())
            try:
                with self.assertRaises(WorkflowBusyError) as caught:
                    WorkflowLease(
                        workspace,
                        runtime_dir=runtime_dir,
                        task="second",
                    ).acquire()
                self.assertEqual(caught.exception.owner["pid"], process.pid)
            finally:
                process.terminate()
                process.join(5)
            self.assertFalse(process.is_alive())
            with WorkflowLease(
                workspace, runtime_dir=runtime_dir, task="after-crash"
            ):
                pass
