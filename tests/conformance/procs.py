"""The one way a test or a script starts a child process.

Why: a test that feeds the tool a producer that never ends (`/dev/zero`, an endless pipe) leaves the tool running at 100% CPU if the runner dies at the wrong
moment (a CI timeout, `kill`, Ctrl-C in the middle of a pipeline, an outer harness that SIGKILLs the runner): the child is orphaned (ppid 1) and nothing
stops it. `subprocess.run(timeout=...)` kills the direct child only, never what it started (`sh -c 'a | b'`, `strace -f`, `perf stat`, `/usr/bin/time`),
and nothing at all happens when the runner itself is gone. This module closes each of those ways:

* every child is a session leader (`start_new_session`), so the child and everything it starts is one process group, and the group is what is killed;
* a hard deadline on every child (`DEFAULT_TIMEOUT`, or the call's own; PROCS_TIMEOUT in the environment changes the default): `run` enforces it as
  `subprocess.run` does, and a watchdog process enforces it for a `Popen` whose reader is blocked in `p.stdout.read()` on an endless producer;
* the group is killed in a `finally` (on any exception, `KeyboardInterrupt` included), when a `with` block ends, when the runner gets SIGTERM, SIGINT or
  SIGHUP, and at exit (atexit), and `kill`, `terminate` and `send_signal` of a `Popen` act on the group;
* the watchdog is a separate process in its own session that is told every group over a pipe and kills all that are still live the moment the pipe closes,
  which is what a runner killed with SIGKILL (or by the OOM killer) does. This is the one case no code in the runner can cover.

`import procs as subprocess` is the whole migration of a file: `run`, `Popen`, `PIPE`, `DEVNULL`, `STDOUT` and the exceptions have the signatures and the
meaning of the standard module's. tests/conformance/test_reaper.py holds the gate, and fails for any file under tests/ or scripts/ that starts a process
any other way.

The one window left: a runner killed between `fork` and the message that tells the watchdog the group (microseconds).
"""

import atexit
import os
import select
import signal
import subprocess as _sp
import sys
import threading
from subprocess import DEVNULL, PIPE, STDOUT, CalledProcessError, CompletedProcess, SubprocessError, TimeoutExpired

__all__ = ["run", "Popen", "kill_all", "PIPE", "DEVNULL", "STDOUT", "CalledProcessError", "CompletedProcess", "SubprocessError", "TimeoutExpired", "DEFAULT_TIMEOUT"]

DEFAULT_TIMEOUT = float(os.environ.get("PROCS_TIMEOUT", "600"))
# the watchdog acts this long after the call's own deadline: the call's own handling (an exception, a message) comes first
GRACE = 5.0

_WATCHDOG = r"""
import os, select, signal, sys, time
for s in (signal.SIGINT, signal.SIGHUP):
    signal.signal(s, signal.SIG_IGN)
groups = {}
def kill(pg):
    try:
        os.killpg(pg, signal.SIGKILL)
    except OSError:
        pass
def term(*_):
    for pg in list(groups):
        kill(pg)
    os._exit(0)
signal.signal(signal.SIGTERM, term)
buf = b""
while True:
    now = time.monotonic()
    for pg, at in list(groups.items()):
        if at <= now:
            kill(pg)
            del groups[pg]
    wait = (min(groups.values()) - now) if groups else None
    ready, _, _ = select.select([0], [], [], None if wait is None else max(wait, 0))
    if not ready:
        continue
    data = os.read(0, 4096)
    if not data:
        break
    buf += data
    *lines, buf = buf.split(b"\n")
    for line in lines:
        op, _, rest = line.partition(b" ")
        if op == b"A":
            pg, _, secs = rest.partition(b" ")
            groups[int(pg)] = time.monotonic() + float(secs)
        elif op == b"D":
            groups.pop(int(rest), None)
for pg in list(groups):
    kill(pg)
"""

_lock = threading.RLock()
_live = set()            # process group ids started here and not yet cleaned
_watchdog = None         # (Popen, write fd) or None
_installed = False


def _tell(line):
    global _watchdog
    with _lock:
        if _watchdog is None:
            try:
                w = _sp.Popen([sys.executable, "-S", "-E", "-c", _WATCHDOG], stdin=_sp.PIPE, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, start_new_session=True)
            except OSError:
                return
            _watchdog = w
        try:
            _watchdog.stdin.write(line)
            _watchdog.stdin.flush()
        except (OSError, ValueError):
            pass


def _killpg(pg, sig=signal.SIGKILL):
    try:
        os.killpg(pg, sig)
    except (ProcessLookupError, PermissionError):
        pass


def kill_all():
    """SIGKILL every group started here that has not been cleaned. Safe to call from a signal handler or atexit."""
    for pg in list(_live):
        _killpg(pg)
    _live.clear()


def _install():
    global _installed
    with _lock:
        if _installed:
            return
        _installed = True
    atexit.register(kill_all)
    if threading.current_thread() is not threading.main_thread():
        return
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name)
        try:
            prev = signal.getsignal(sig)
            if prev == signal.SIG_IGN or (sig == signal.SIGINT and callable(prev)):
                continue          # ignored stays ignored; KeyboardInterrupt unwinds through the `finally` blocks, and atexit follows

            def handler(num, frame, prev=prev):
                kill_all()
                if callable(prev):
                    return prev(num, frame)
                signal.signal(num, signal.SIG_DFL)
                os.kill(os.getpid(), num)

            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


class Popen(_sp.Popen):
    """`subprocess.Popen` whose child leads a process group of its own that is killed with it, within a deadline (`deadline=` seconds, default
    DEFAULT_TIMEOUT) that holds even while the caller is blocked reading its output."""

    def __init__(self, *args, deadline=None, **kw):
        _install()
        self._pg = None
        kw.setdefault("start_new_session", True)
        super().__init__(*args, **kw)
        self._pg = self.pid if kw["start_new_session"] else None
        if self._pg is not None:
            with _lock:
                _live.add(self._pg)
            _tell(b"A %d %f\n" % (self._pg, (DEFAULT_TIMEOUT if deadline is None else deadline) + GRACE))

    def kill_group(self):
        if self._pg is not None:
            _killpg(self._pg)

    def wait(self, timeout=None):
        try:
            return super().wait(timeout)
        finally:
            if self.returncode is not None:
                self.release()

    def poll(self):
        try:
            return super().poll()
        finally:
            if self.returncode is not None:
                self.release()

    def release(self):
        """The group is gone: no process of it may remain, and the watchdog forgets it."""
        if getattr(self, "_pg", None) is not None:
            _killpg(self._pg)
            with _lock:
                _live.discard(self._pg)
            _tell(b"D %d\n" % self._pg)
            self._pg = None

    def send_signal(self, sig):
        if self._pg is not None and sig in (signal.SIGKILL, signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            _killpg(self._pg, sig)
        else:
            super().send_signal(sig)

    def kill(self):
        self.kill_group()
        super().kill()

    def terminate(self):
        self.kill_group()          # a group is not asked politely: what it leaves behind is the point
        super().terminate()

    def __exit__(self, *exc):
        try:
            self.kill_group()
        finally:
            try:
                super().__exit__(*exc)
            finally:
                self.release()

    def __del__(self, *a, **k):
        try:
            self.release()
        except Exception:
            pass
        super().__del__(*a, **k)


def run(*popenargs, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    """`subprocess.run`, but the child's whole group is killed on a timeout, on any exception and when the call ends, and a call has a deadline
    (`timeout`, else DEFAULT_TIMEOUT)."""
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used.")
        kwargs["stdin"] = PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output.")
        kwargs["stdout"] = PIPE
        kwargs["stderr"] = PIPE
    limit = DEFAULT_TIMEOUT if timeout is None else timeout
    with Popen(*popenargs, deadline=limit, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=limit)
        except TimeoutExpired as exc:
            process.kill_group()
            exc.stdout, exc.stderr = process.communicate()[0], None
            raise
        except BaseException:
            process.kill_group()
            raise
        retcode = process.poll()
        if check and retcode:
            raise CalledProcessError(retcode, process.args, output=stdout, stderr=stderr)
    return CompletedProcess(process.args, retcode, stdout, stderr)
