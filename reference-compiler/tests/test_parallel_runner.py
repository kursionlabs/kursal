"""The parallel wrapper must preserve unittest failures and process isolation."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.test import ROOT, run_tests


class ParallelRunnerTests(unittest.TestCase):
    def test_workers_overlap_in_separate_processes(self):
        with tempfile.TemporaryDirectory(prefix="kursal-runner-") as base:
            root = Path(base)
            for name, other in (("first", "second"), ("second", "first")):
                (root / f"{name}.py").write_text(f'''
import os
from pathlib import Path
import time
import unittest
class Worker(unittest.TestCase):
    def test_overlap(self):
        Path("{name}.started").write_text(str(os.getpid()))
        deadline = time.monotonic() + 10
        while not Path("{other}.started").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(Path("{other}.started").exists())
        self.assertNotEqual(Path("{other}.started").read_text(), str(os.getpid()))
''')
            output = io.StringIO()
            with redirect_stdout(output):
                result = run_tests(["first", "second"], 2, root)
            self.assertEqual(result, 0, output.getvalue())
            self.assertIn('2/2 test groups passed', output.getvalue())

    def test_failure_and_import_error_are_reported(self):
        with tempfile.TemporaryDirectory(prefix="kursal-runner-") as base:
            root = Path(base)
            (root / "failure.py").write_text('''import unittest
class Failure(unittest.TestCase):
    def test_fail(self):
        self.fail("intentional failure")
''')
            output = io.StringIO()
            with redirect_stdout(output):
                result = run_tests(["failure", "missing_module"], 2, root)
            self.assertEqual(result, 1)
            self.assertIn('intentional failure', output.getvalue())
            self.assertIn('ModuleNotFoundError', output.getvalue())
            self.assertIn('0/2 test groups passed', output.getvalue())

    def test_cli_exit_status(self):
        for args, expected in ((["-j", "0"], 2), (["missing_test_module"], 1)):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(ROOT / "tools/test.py"), *args],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
