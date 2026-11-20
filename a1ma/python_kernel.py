"""Persistent Python kernel — state survives across calls (gptme-style).

A long-lived subprocess executes code in one shared namespace, so variables,
imports, and definitions persist between `python` tool calls.
"""
import json
import queue
import subprocess
import sys
import threading

DRIVER = r'''
import sys, json, io, ast, traceback
from contextlib import redirect_stdout, redirect_stderr

ns = {"__name__": "__main__"}

def run(code):
    out, err = io.StringIO(), io.StringIO()
    try:
        tree = ast.parse(code)
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            *rest, last = tree.body
            if rest:
                exec(compile(ast.Module(body=rest, type_ignores=[]), "<a1ma>", "exec"), ns)
            with redirect_stdout(out), redirect_stderr(err):
                result = eval(compile(ast.Expression(last.value), "<a1ma>", "eval"), ns)
            r = repr(result)
            if r != "None":
                out.write(r + "\n")
        else:
            with redirect_stdout(out), redirect_stderr(err):
                exec(compile(code, "<a1ma>", "exec"), ns)
        return {"ok": True, "output": out.getvalue() + err.getvalue()}
    except Exception:
        return {"ok": False, "output": out.getvalue() + err.getvalue() + traceback.format_exc()}

while True:
    line = sys.stdin.readline()
    if not line:
        break
    try:
        n = int(line.strip())
    except ValueError:
        break
    code = sys.stdin.read(n)
    payload = json.dumps(run(code))
    sys.stdout.write(str(len(payload)) + "\n" + payload)
    sys.stdout.flush()
'''


class KernelDead(Exception):
    pass


class PythonKernel:
    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._spawn()

    def _spawn(self):
        self._proc = subprocess.Popen(
            [sys.executable, "-u", "-c", DRIVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self._queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while True:
                len_line = self._proc.stdout.readline()
                if not len_line:
                    break
                n = int(len_line.strip())
                payload = self._proc.stdout.read(n)
                self._queue.put(json.loads(payload))
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    def run(self, code: str) -> str:
        with self._lock:
            if self._proc.poll() is not None:
                self._spawn()
            try:
                self._proc.stdin.write(f"{len(code)}\n{code}")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self._spawn()
                return f"[kernel restarted after pipe error: {e}]"
            try:
                res = self._queue.get(timeout=self.timeout)
            except queue.Empty:
                # kernel is wedged (e.g. input() or runaway loop) — kill and restart
                self._proc.kill()
                self._spawn()
                return f"[execution timed out after {self.timeout}s; kernel restarted, state lost]"
            return res["output"].rstrip() or "[no output]"

    def restart(self) -> str:
        with self._lock:
            self._proc.kill()
            self._spawn()
            return "kernel restarted, state cleared"


_kernel: PythonKernel | None = None


def python_exec(code: str) -> str:
    global _kernel
    if _kernel is None:
        _kernel = PythonKernel()
    return _kernel.run(code)


def python_restart() -> str:
    global _kernel
    if _kernel is None:
        _kernel = PythonKernel()
    return _kernel.restart()
