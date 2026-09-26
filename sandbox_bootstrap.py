"""Trusted bootstrap for the Answer-mode sandbox (SP3 A5 + C6). STDLIB ONLY.

``sandbox.verify_python`` spawns ``<base python> -I -X utf8 -B sandbox_bootstrap.py
<solution.py> <run_dir> <config-json>`` instead of running the untrusted
solution directly, and this file is what runs first in the child:

1. **Audit hook (C6).** Unless the config says ``"audit": false`` (tests of
   the other containment layers only), install a ``sys.addaudithook`` hook
   that refuses, with a ``PermissionError`` naming the sandbox:

   * opening a file for WRITING (or deleting / renaming / chmod-ing /
     creating dirs) anywhere outside the run dir;
   * opening or listing anything under a known secret path (the parent
     passes the list: ``~/.claude``, the repo ``.env``, credential stores...);
   * network: ``socket.connect`` / ``bind`` / ``sendto`` / ``sendmsg`` and
     name resolution;
   * process creation: ``subprocess``, ``os.system`` / ``popen``, ``os.exec*``,
     ``os.spawn*``, ``posix_spawn``, ``fork``, ``_winapi.CreateProcess``,
     ``os.startfile``, and ``os.kill``;
   * ``ctypes`` library loading / symbol lookup / raw memory access, symlink /
     hardlink / junction creation, and registry writes.

   This is **defence in depth, not a security boundary** (see SECURITY.md):
   CPython's own docs say audit hooks cannot sandbox hostile code, and there
   are known gaps (e.g. ``dir_fd``-relative opens on POSIX). It exists to stop
   careless or prompt-injected solution code from touching credentials, the
   network or the rest of the disk.
2. **Handshake (A5).** Block on an unbuffered ONE-byte ``os.read(0, 1)`` until
   the parent sends the go byte. The parent sends it only AFTER it has put this
   process into the Windows Job Object, so no untrusted line can allocate or
   spawn outside the memory / process caps, however long a GIL-starved parent
   takes to get to ``AssignProcessToJobObject``. The raw 1-byte read leaves
   the rest of stdin (the sample input) untouched in the pipe, so every way a
   solution reads stdin — ``input()``, ``sys.stdin``, ``sys.stdin.buffer``,
   ``open(0)``, ``os.read(0, n)`` — sees exactly the sample and nothing else.
3. Make the solution feel like ``python solution.py``: ``__name__ ==
   "__main__"``, ``sys.argv == [solution.py]``, the run dir first on
   ``sys.path``, then ``runpy.run_path``. Exit codes and tracebacks propagate
   unchanged (an uncaught exception still exits 1 with a traceback).

It must stay tiny and trusted: it runs under ``-I`` (no user site, no
``PYTHON*`` env vars, script dir not on ``sys.path``), so it imports nothing
from the repo.
"""
import _thread
import json
import os
import runpy
import sys
import traceback

GO = b"\x01"
EXIT_NO_GO = 97  # parent vanished / closed stdin before releasing us

# Refused outright, whatever the arguments.
_ALWAYS_BLOCKED = frozenset({
    # process creation / signalling
    "subprocess.Popen", "_winapi.CreateProcess", "os.system", "os.exec",
    "os.spawn", "os.posix_spawn", "os.fork", "os.forkpty", "os.startfile",
    "os.kill", "os.killpg", "pty.spawn",
    # network (incl. DNS, which can exfiltrate on its own)
    "socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg",
    "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
    "socket.gethostbyaddr", "socket.getnameinfo",
    # ctypes: loading libraries, resolving symbols, poking memory
    "ctypes.dlopen", "ctypes.dlsym", "ctypes.dlsym/handle", "ctypes.call_function",
    "ctypes.cdata", "ctypes.cdata/buffer", "ctypes.string_at", "ctypes.wstring_at",
    # links / junctions could point a run-dir path anywhere on disk
    "os.symlink", "os.link", "_winapi.CreateJunction",
    # registry writes (e.g. a Run key)
    "winreg.CreateKey", "winreg.SetValue", "winreg.DeleteKey", "winreg.DeleteValue",
})

# Path-mutating events -> indexes of their path arguments. Allowed only when
# every such path is inside the run dir.
_MUTATING = {
    "os.remove": (0,), "os.rmdir": (0,), "os.mkdir": (0,), "os.rename": (0, 1),
    "os.chmod": (0,), "os.chown": (0,), "os.chflags": (0,), "os.truncate": (0,),
    "os.utime": (0,), "os.setxattr": (0,), "os.removexattr": (0,),
    "shutil.rmtree": (0,), "shutil.copyfile": (1,), "shutil.copytree": (1,),
    "shutil.move": (1,),
}

# Listing events -> index of the path argument. Refused under a secret path.
_LISTING = {"os.listdir": 0, "os.scandir": 0, "glob.glob": 0, "os.chdir": 0}

_WRITE_FLAGS = (
    os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    | getattr(os, "O_EXCL", 0)
)
_GENERIC_WRITE_ACCESS = 0x40000000 | 0x00010000 | 0x00000002  # GENERIC_WRITE|DELETE|FILE_WRITE_DATA


def _deny(event: str, what: str = "") -> None:
    suffix = f" ({what})" if what else ""
    raise PermissionError(f"LeetCoach sandbox: {event} is blocked{suffix}")


def install_audit_hook(run_dir: str, secret_paths: list) -> None:
    """Install the C6 hook. Called before any untrusted code exists."""
    normcase, realpath, fsdecode = os.path.normcase, os.path.realpath, os.fsdecode
    sep = os.sep

    def norm(path) -> str:
        return normcase(realpath(fsdecode(path)))

    def under(path: str, root: str) -> bool:
        return path == root or path.startswith(root.rstrip(sep) + sep)

    run = norm(run_dir)
    secrets = tuple(norm(p) for p in secret_paths if p)
    devnull = normcase(os.devnull)
    local = _thread._local()  # per-thread re-entrancy guard (realpath -> events)

    def path_of(arg):
        """A normalized path, or None for an fd / None / non-path argument."""
        if arg is None or isinstance(arg, int):
            return None
        try:
            if normcase(fsdecode(arg)) == devnull:
                return None
            return norm(arg)
        except (TypeError, ValueError, OSError):
            return "<unresolvable>"  # fails closed: neither in run dir nor secret-safe

    def check_secret(event: str, path) -> None:
        if path is not None and any(under(path, s) for s in secrets):
            _deny(event, "secret path")

    def check_inside_run(event: str, path) -> None:
        if path is not None and not under(path, run):
            _deny(event, "outside the run directory")

    def on_open(event: str, args) -> None:
        path = path_of(args[0])
        check_secret(event, path)
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
        )
        if writing:
            check_inside_run("open for writing", path)

    def on_create_file(event: str, args) -> None:  # _winapi.CreateFile
        path = path_of(args[0])
        check_secret(event, path)
        access = args[1] if len(args) > 1 else 0
        if isinstance(access, int) and access & _GENERIC_WRITE_ACCESS:
            if path is not None and not path.startswith("\\\\.\\pipe\\"):
                check_inside_run(event, path)

    def hook(event: str, args) -> None:
        if event in _ALWAYS_BLOCKED:
            _deny(event)
        if event == "open":
            handler = on_open
        elif event == "_winapi.CreateFile":
            handler = on_create_file
        elif event in _MUTATING or event in _LISTING:
            handler = None
        else:
            return
        if getattr(local, "busy", False):
            return  # an event raised by our own path resolution
        local.busy = True
        try:
            if handler is not None:
                handler(event, args)
            elif event in _MUTATING:
                for i in _MUTATING[event]:
                    if i < len(args):
                        path = path_of(args[i])
                        check_secret(event, path)
                        check_inside_run(event, path)
            else:
                i = _LISTING[event]
                if i < len(args):
                    check_secret(event, path_of(args[i]))
        finally:
            local.busy = False

    sys.addaudithook(hook)


def main(argv: list) -> None:
    script, run_dir, raw_cfg = argv[1], argv[2], argv[3]
    cfg = json.loads(raw_cfg)

    if cfg.get("audit", True):
        install_audit_hook(run_dir, list(cfg.get("secret_paths", [])))

    # The handshake. Nothing below this line runs until the parent has
    # assigned the job object.
    if os.read(0, 1) != GO:
        os._exit(EXIT_NO_GO)

    # Run the untrusted solution as if it were the main script.
    sys.argv[:] = [script]
    sys.path.insert(0, run_dir)
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - mirror the interpreter's own handler
        # Same exit code (1) and traceback as `python solution.py`, minus the
        # bootstrap/runpy frames, so the user's stderr shows only their code.
        tb = exc.__traceback__
        while tb is not None and tb.tb_frame.f_code.co_filename != script:
            tb = tb.tb_next
        traceback.print_exception(type(exc), exc, tb)
        sys.stderr.flush()
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
