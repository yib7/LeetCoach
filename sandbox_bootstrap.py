"""Trusted bootstrap for the Answer-mode sandbox (SP3 A5 + C6). STDLIB ONLY.

``sandbox.verify_python`` spawns ``<base python> -I -X utf8 -B sandbox_bootstrap.py``
instead of running the untrusted solution directly, and this file is what runs
first in the child. Its stdin carries, in order: the length-prefixed JSON
config (script path, run dir, secret paths, rlimits; kept OFF the command line
so ``sys.orig_argv`` does not reveal it), the go byte, then the sample input.

0. **Config + POSIX rlimits.** :func:`read_config` reads exactly the framed
   config. On POSIX :func:`apply_rlimits` then sets the memory / CPU /
   file-size / process limits in this process (soft == hard), replacing a
   ``preexec_fn`` that is not fork-safe in the threaded parent. If a required
   limit cannot be set the bootstrap exits :data:`EXIT_NO_CAPS` with
   :data:`CAPS_MARKER` on stderr and the solution never runs (fail closed).
1. **Audit hook (C6).** Unless the config says ``"audit": false`` (tests of
   the other containment layers only), install a ``sys.addaudithook`` hook
   that refuses, with a ``PermissionError`` naming the sandbox:

   * opening a file for WRITING (or deleting / renaming / chmod-ing /
     creating dirs) anywhere outside the run dir;
   * opening or listing anything under a known secret path (the parent
     passes the list: ``~/.claude``, the repo ``.env``, credential stores...);
   * network, with no exceptions: ``socket.bind`` / ``connect`` / ``sendto``
     / ``sendmsg`` and name resolution, whatever the address (loopback
     included). That also rules out ``socket.socketpair()`` on Windows (it
     binds a loopback listener), so asyncio, whose Windows event loop needs
     one for its self-pipe, is unavailable: ``asyncio.run`` ends the run as
     "blocked by sandbox (socket.bind)". An earlier socketpair allowance was
     dropped after it was bypassed three times (the last by a socket subclass
     faking ``getsockname``); LeetCode solutions need neither sockets nor
     asyncio;
   * SQLite entirely (``sqlite3.connect``, extension loading): ``ATTACH
     DATABASE`` writes wherever it is told without an audited ``open``, and
     extensions are native code, so no per-path check could hold;
   * process creation: ``subprocess``, ``os.system`` / ``popen``, ``os.exec*``,
     ``os.spawn*``, ``posix_spawn``, ``fork``, ``_winapi.CreateProcess``,
     ``os.startfile``, and ``os.kill``; on POSIX also
     ``_posixsubprocess.fork_exec``, which raises no audit event of its own
     (``subprocess`` audits before calling it, but ``multiprocessing``'s spawn
     and forkserver starters and its resource tracker call it directly, so a
     ``multiprocessing`` solution on macOS would otherwise start an
     interpreter without this hook);
   * ``ctypes`` library loading / symbol lookup / raw memory access, symlink /
     hardlink / junction creation, registry writes, and heap walking via
     ``gc.get_objects`` / ``get_referrers`` / ``get_referents``.

   This is **defence in depth, not a security boundary** (see SECURITY.md):
   CPython's own docs say audit hooks cannot sandbox hostile code, and there
   are known gaps (e.g. ``dir_fd``-relative opens on POSIX). It exists to stop
   careless or prompt-injected solution code from touching credentials, the
   network or the rest of the disk.
2. **Handshake (A5).** Write the one-byte :data:`READY` signal to stdout,
   then block on an unbuffered ONE-byte ``os.read(0, 1)`` until the parent
   sends the go byte. The parent reads READY before it sends go, so an exit
   BEFORE READY is known to be the bootstrap's own (bad config, caps that
   could not be applied: the solution never ran), and nothing the solution
   does after go can pass for one. The parent sends go only AFTER it has put
   this process into the Windows Job Object, so no untrusted line can
   allocate or spawn outside the memory / process caps, however long a
   GIL-starved parent takes to get to ``AssignProcessToJobObject``. The config and the go byte
   are read with raw, exact-length reads that leave the rest of stdin (the
   sample input) untouched in the pipe, so every way a solution reads stdin
   (``input()``, ``sys.stdin``, ``sys.stdin.buffer``, ``open(0)``,
   ``os.read(0, n)``) sees exactly the sample and nothing else. Tests can ask
   for a ``ready_marker`` file to be created right before the go-byte read,
   as an explicit pre-go signal.
3. Make the solution feel like ``python solution.py``: ``__name__ ==
   "__main__"``, ``sys.argv == [solution.py]``, the run dir first on
   ``sys.path``, then ``runpy.run_path``. Exit codes and tracebacks propagate
   unchanged (an uncaught exception still exits 1 with a traceback).

It must stay tiny and trusted: it runs under ``-I`` (no user site, no
``PYTHON*`` env vars, script dir not on ``sys.path``), so it imports nothing
from the repo. (``sandbox.py`` imports the constants below from here, never
the other way round.)
"""
import _thread
import json
import os
import runpy
import stat
import sys
import traceback

# The go byte. ``sandbox.py`` imports this constant, so parent and child can
# never disagree on it.
GO = b"\x01"
# Written to stdout right before the go-byte read ("config read, caps and hook
# in place, parked at the handshake"). The parent waits for it before sending
# go, so an exit with no READY is a bootstrap failure, never the solution's.
READY = b"\x02"
EXIT_NO_GO = 97  # parent vanished / closed stdin before releasing us
EXIT_NO_CAPS = 98  # a required POSIX rlimit could not be set: fail closed
DENY_PREFIX = "LeetCoach sandbox:"
CAPS_MARKER = f"{DENY_PREFIX} resource limits could not be applied"
# macOS often refuses RLIMIT_AS; there the memory cap is best-effort.
RLIMIT_AS_SKIPPED_MARKER = (
    f"{DENY_PREFIX} RLIMIT_AS could not be set on macOS; memory cap is best-effort"
)

# Framed config: a 4-byte big-endian length, then that many bytes of UTF-8
# JSON. Bounded so a corrupt header cannot make us allocate gigabytes.
CONFIG_LEN_BYTES = 4
_MAX_CONFIG_BYTES = 1 << 20

# POSIX rlimits the parent may configure -> required? NPROC counts every
# process of the user and is not adjustable in some containers, so failing to
# set it is tolerated (as the old preexec_fn did); the others are the memory /
# CPU / disk caps.
_RLIMITS = {"AS": True, "CPU": True, "FSIZE": True, "NPROC": False}

# POSIX names that are aliases of an already-open fd rather than files.
_STD_ALIASES = frozenset({"/dev/stdin", "/dev/stdout", "/dev/stderr"})
_FD_ALIAS_DIRS = ("/dev/fd/", "/proc/self/fd/")
_DEV_FD = "/dev/fd/"
_ANON_FD_KINDS = ("pipe:[", "socket:[", "anon_inode:")

# Refused outright, whatever the arguments.
_ALWAYS_BLOCKED = frozenset({
    # process creation / signalling
    "subprocess.Popen", "_winapi.CreateProcess", "os.system", "os.exec",
    "os.spawn", "os.posix_spawn", "os.fork", "os.forkpty", "os.startfile",
    "os.kill", "os.killpg", "pty.spawn",
    # network, loopback included (DNS too: it can exfiltrate on its own).
    # No socketpair allowance: it was bypassed three times, so asyncio (which
    # binds one on Windows) is simply unavailable in the sandbox.
    "socket.bind", "socket.connect", "socket.sendto", "socket.sendmsg",
    "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
    "socket.gethostbyaddr", "socket.getnameinfo",
    # ctypes: loading libraries, resolving symbols, poking memory
    "ctypes.dlopen", "ctypes.dlsym", "ctypes.dlsym/handle", "ctypes.call_function",
    "ctypes.cdata", "ctypes.cdata/buffer", "ctypes.string_at", "ctypes.wstring_at",
    # links / junctions could point a run-dir path anywhere on disk
    "os.symlink", "os.link", "_winapi.CreateJunction",
    # registry writes (e.g. a Run key)
    "winreg.CreateKey", "winreg.SetValue", "winreg.DeleteKey", "winreg.DeleteValue",
    # heap walking (could reach objects the bootstrap holds)
    "gc.get_objects", "gc.get_referrers", "gc.get_referents",
    # SQLite: ATTACH DATABASE writes anywhere (sqlite's own I/O, no audited
    # open) and extensions are native code, so it is refused outright.
    "sqlite3.connect", "sqlite3.enable_load_extension", "sqlite3.load_extension",
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
    raise PermissionError(f"{DENY_PREFIX} {event} is blocked{suffix}")


def fd_is_stream(fd: int) -> bool:
    """Is open fd ``fd`` a pipe, socket or terminal (never a regular file)?
    False when it is not open."""
    try:
        mode = os.fstat(fd).st_mode
    except (OSError, ValueError, OverflowError):
        return False
    if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return True
    try:
        return os.isatty(fd)
    except (OSError, ValueError, OverflowError):
        return False


def is_stream_alias(raw: str, resolved: str, fd_probe=None) -> bool:
    """POSIX: is ``raw`` a std-stream / fd alias (``/dev/stdin``,
    ``/dev/stdout``, ``/dev/stderr``, ``/dev/fd/N``, ``/proc/self/fd/N``)
    that ``resolved`` (its realpath) shows to be a pipe, socket or terminal?

    Such a path is exempt from the write check, like ``os.devnull``. The
    resolved target matters: on Linux, opening ``/proc/self/fd/N`` (or
    ``/dev/stdout`` after an ``os.dup2``) REOPENS the underlying file with
    new flags, so an alias that points at a real file is still checked as
    that file. On macOS ``/dev/fd/N`` is a device node, not a symlink, so
    ``realpath('/dev/stdout')`` stops at ``/dev/fd/1``; that fd is then asked
    what it is (``fd_probe``, default :func:`fd_is_stream`). String logic
    apart from that probe, so it is unit-testable on any OS.
    """
    if raw not in _STD_ALIASES:
        for prefix in _FD_ALIAS_DIRS:
            num = raw[len(prefix):]
            if raw.startswith(prefix) and num.isascii() and num.isdigit():
                break
        else:
            return False
    head, _, tail = resolved.rpartition("/")
    if resolved.startswith("/proc/") and head.endswith("/fd") and tail.startswith(_ANON_FD_KINDS):
        return True
    if resolved.startswith(_DEV_FD):
        num = resolved[len(_DEV_FD):]
        if not (num.isascii() and num.isdigit()):
            return False
        return bool((fd_probe or fd_is_stream)(int(num)))
    return resolved.startswith("/dev/pts/") or resolved in ("/dev/tty", "/dev/null")


def _deny_fork_exec() -> None:
    """POSIX: swap ``_posixsubprocess.fork_exec`` for a denial before any
    untrusted code exists. It raises no audit event, and ``multiprocessing``
    (spawn / forkserver starters, resource tracker) calls it directly. A later
    fresh import of the module is refused by the hook ("import" event)."""
    try:
        import _posixsubprocess
    except ImportError:
        return

    def fork_exec(*_args, **_kwargs):
        _deny("_posixsubprocess.fork_exec")

    _posixsubprocess.fork_exec = fork_exec


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
    posix = os.name != "nt"
    local = _thread._local()  # per-thread re-entrancy guard (realpath -> events)

    def path_of(arg):
        """A normalized path, or None for an fd / None / non-path argument."""
        if arg is None or isinstance(arg, int):
            return None
        try:
            raw = fsdecode(arg)
            if normcase(raw) == devnull:
                return None
            if posix and is_stream_alias(raw, realpath(raw)):
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
        if (isinstance(access, int) and access & _GENERIC_WRITE_ACCESS
                and path is not None and not path.startswith("\\\\.\\pipe\\")):
            check_inside_run(event, path)

    handlers = {
        "open": on_open,
        "_winapi.CreateFile": on_create_file,
    }

    def hook(event: str, args) -> None:
        if event in _ALWAYS_BLOCKED:
            _deny(event)
        if event == "import" and args and args[0] == "_posixsubprocess":
            _deny("_posixsubprocess.fork_exec", "fresh import")
        handler = handlers.get(event)
        if handler is None and event not in _MUTATING and event not in _LISTING:
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

    if posix:
        _deny_fork_exec()
    sys.addaudithook(hook)


def _read_exact(fd: int, n: int) -> bytes:
    """Exactly ``n`` bytes from ``fd`` with raw reads (never over-consuming,
    so the go byte and the sample stay in the pipe). EOFError if it closes
    first."""
    buf = bytearray()
    while len(buf) < n:
        chunk = os.read(fd, n - len(buf))
        if not chunk:
            raise EOFError("stdin closed inside the bootstrap config")
        buf += chunk
    return bytes(buf)


def read_config(fd: int = 0) -> dict:
    """Read the parent's framed config (``sandbox._frame_config``)."""
    size = int.from_bytes(_read_exact(fd, CONFIG_LEN_BYTES), "big")
    if size > _MAX_CONFIG_BYTES:
        raise ValueError(f"bootstrap config too large ({size} bytes)")
    cfg = json.loads(_read_exact(fd, size).decode("utf-8"))
    if not isinstance(cfg, dict):
        raise TypeError("bootstrap config is not an object")
    return cfg


def apply_rlimits(limits: dict, resource_mod=None) -> None:
    """POSIX: set each configured limit with soft == hard, so the solution
    cannot raise it. A required limit that cannot be set raises; the
    optional NPROC is skipped. Runs before the go byte, so the caps exist
    before any untrusted line.

    macOS often refuses ``RLIMIT_AS``; there (and only there) a failed AS is
    reported on stderr (:data:`RLIMIT_AS_SKIPPED_MARKER`) and skipped, so the
    memory cap is best-effort on macOS. CPU / FSIZE stay required everywhere
    and AS stays required on Linux (Windows' cap is the parent's Job
    Object)."""
    if resource_mod is None:
        import resource as resource_mod  # POSIX only
    for name, value in limits.items():
        required = _RLIMITS.get(name)
        if required is None:
            raise ValueError(f"unknown rlimit {name!r}")
        try:
            resource_mod.setrlimit(getattr(resource_mod, f"RLIMIT_{name}"), (value, value))
        except (ValueError, OSError, AttributeError) as exc:
            if name == "AS" and sys.platform == "darwin":
                sys.stderr.write(f"{RLIMIT_AS_SKIPPED_MARKER} ({type(exc).__name__}: {exc})\n")
                sys.stderr.flush()
                continue
            if required:
                raise


def main(argv: list) -> None:
    try:
        cfg = read_config(0)
        script, run_dir = cfg["script"], cfg["run_dir"]
    except (OSError, ValueError, EOFError, KeyError, TypeError):
        os._exit(EXIT_NO_GO)

    if os.name != "nt":
        try:
            apply_rlimits(dict(cfg.get("rlimits") or {}))
        except Exception as exc:  # noqa: BLE001 - any failure means "uncapped"
            sys.stderr.write(f"{CAPS_MARKER} ({type(exc).__name__}: {exc})\n")
            sys.stderr.flush()
            os._exit(EXIT_NO_CAPS)

    if cfg.get("audit", True):
        install_audit_hook(run_dir, list(cfg.get("secret_paths", [])))

    ready_marker = cfg.get("ready_marker")  # tests only: explicit pre-go signal
    if ready_marker:
        open(ready_marker, "wb").close()

    # The handshake. Tell the parent we are parked here (it reads READY
    # before it sends go, so an exit before READY is ours, never the
    # solution's), then wait. Nothing below this line runs until the parent
    # has assigned the job object.
    try:
        os.write(1, READY)
    except OSError:
        os._exit(EXIT_NO_GO)
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
