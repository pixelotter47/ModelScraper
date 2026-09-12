import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from runtime_lock import WorkflowBusyError, WorkflowLease


def windows_short_path(path):
    """Return a real alternate spelling, when the Windows volume provides one."""
    if os.name != "nt":
        raise unittest.SkipTest("Windows short-path regression")
    import ctypes

    resolved = Path(path).resolve()
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(resolved), buffer, len(buffer))
    if not length or length >= len(buffer):
        raise unittest.SkipTest("Windows short paths are unavailable")
    alias = Path(buffer.value)
    if os.path.normcase(str(alias)) == os.path.normcase(str(resolved)):
        raise unittest.SkipTest("This volume does not provide an alternate short path")
    if alias.resolve() != resolved:
        raise AssertionError("Short-path fixture must resolve to its original directory")
    return alias


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
    def test_short_path_alias_cannot_acquire_a_second_workspace_lease(self):
        with tempfile.TemporaryDirectory(prefix="modelscraper-long-workspace-") as tmp:
            workspace = Path(tmp).resolve()
            alias = windows_short_path(workspace)
            runtime_dir = workspace / "runtime"
            with WorkflowLease(workspace, runtime_dir=runtime_dir):
                with self.assertRaises(WorkflowBusyError):
                    with WorkflowLease(alias, runtime_dir=runtime_dir):
                        pass
            with WorkflowLease(alias, runtime_dir=runtime_dir):
                pass

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
