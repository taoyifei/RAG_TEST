"""在独立子进程中为 LibreOffice 建立最小权限沙箱。"""

from __future__ import annotations

import argparse
import ctypes
import errno
import os
import resource
import sys
from pathlib import Path

_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_SECCOMP_ACTION_ALLOW = 0x7FFF0000
_SECCOMP_ACTION_ERRNO = 0x00050000
_SANDBOX_UNAVAILABLE = 78
_LANDLOCK_ABI_REFER = 2
_LANDLOCK_ABI_TRUNCATE = 3

_ACCESS_EXECUTE = 1 << 0
_ACCESS_WRITE_FILE = 1 << 1
_ACCESS_READ_FILE = 1 << 2
_ACCESS_READ_DIR = 1 << 3
_ACCESS_REMOVE_DIR = 1 << 4
_ACCESS_REMOVE_FILE = 1 << 5
_ACCESS_MAKE_CHAR = 1 << 6
_ACCESS_MAKE_DIR = 1 << 7
_ACCESS_MAKE_REG = 1 << 8
_ACCESS_MAKE_SOCK = 1 << 9
_ACCESS_MAKE_FIFO = 1 << 10
_ACCESS_MAKE_BLOCK = 1 << 11
_ACCESS_MAKE_SYM = 1 << 12
_ACCESS_REFER = 1 << 13
_ACCESS_TRUNCATE = 1 << 14
_READ_ONLY_ACCESS = _ACCESS_EXECUTE | _ACCESS_READ_FILE | _ACCESS_READ_DIR
_WRITE_ACCESS_V1 = (
    _READ_ONLY_ACCESS
    | _ACCESS_WRITE_FILE
    | _ACCESS_REMOVE_DIR
    | _ACCESS_REMOVE_FILE
    | _ACCESS_MAKE_CHAR
    | _ACCESS_MAKE_DIR
    | _ACCESS_MAKE_REG
    | _ACCESS_MAKE_SOCK
    | _ACCESS_MAKE_FIFO
    | _ACCESS_MAKE_BLOCK
    | _ACCESS_MAKE_SYM
)
_DENIED_SYSCALLS = (
    "socket",
    "socketpair",
    "connect",
    "bind",
    "listen",
    "accept",
    "accept4",
    "sendto",
    "sendmsg",
    "sendmmsg",
    "recvfrom",
    "recvmsg",
    "recvmmsg",
    "shutdown",
    "ptrace",
    "mount",
    "umount2",
    "pivot_root",
    "unshare",
    "setns",
    "bpf",
    "perf_event_open",
    "userfaultfd",
    "keyctl",
    "request_key",
    "add_key",
    "open_by_handle_at",
    "init_module",
    "finit_module",
    "delete_module",
    "kexec_load",
    "reboot",
    "swapon",
    "swapoff",
)


class _LandlockRulesetAttr(ctypes.Structure):
    """Linux landlock_ruleset_attr 的 ctypes 映射。"""

    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    """Linux landlock_path_beneath_attr 的 ctypes 映射。"""

    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
    ]


def main() -> int:
    """解析受控参数、应用沙箱并替换为 LibreOffice。

    Args:
        无参数；只读取固定命令行参数。

    Returns:
        LibreOffice 进程退出码；沙箱不可用时返回 78。

    """
    arguments = _arguments()
    paths = _validated_paths(arguments)
    _set_resource_limits(arguments)
    try:
        _restrict_filesystem(paths)
        _restrict_syscalls()
    except OSError:
        return _SANDBOX_UNAVAILABLE
    command = (
        arguments.executable,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nofirststartwizard",
        "--norestore",
        "--nolockcheck",
        f"-env:UserInstallation={paths['profile'].as_uri()}",
        "--convert-to",
        "docx:Office Open XML Text",
        "--outdir",
        str(paths["output"]),
        str(paths["input"]),
    )
    environment = {
        "DBUS_SESSION_BUS_ADDRESS": "/dev/null",
        "HOME": str(paths["profile"]),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "SAL_DISABLE_SYNCHRONOUS_PRINTER_DETECTION": "1",
        "SAL_USE_VCLPLUGIN": "svp",
        "TMPDIR": str(paths["temporary"]),
    }
    os.chdir(paths["temporary"])
    os.execve(arguments.executable, command, environment)  # noqa: S606
    return 1


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--temporary", required=True)
    parser.add_argument("--address-space-bytes", required=True, type=int)
    parser.add_argument("--file-size-bytes", required=True, type=int)
    parser.add_argument("--cpu-seconds", required=True, type=int)
    return parser.parse_args()


def _validated_paths(arguments: argparse.Namespace) -> dict[str, Path]:
    root = Path(arguments.input).resolve(strict=True).parent.parent
    paths = {
        "input": Path(arguments.input).resolve(strict=True),
        "output": Path(arguments.output).resolve(strict=True),
        "profile": Path(arguments.profile).resolve(strict=True),
        "temporary": Path(arguments.temporary).resolve(strict=True),
    }
    if (
        not Path(arguments.executable).is_absolute()
        or not Path(arguments.executable).is_file()
        or not paths["input"].is_file()
        or any(
            not paths[name].is_dir()
            for name in ("output", "profile", "temporary")
        )
        or paths["input"].parent.parent != root
        or any(
            paths[name].parent != root
            for name in ("output", "profile", "temporary")
        )
        or len(set(paths.values())) != len(paths)
    ):
        raise ValueError("DOC converter 路径合同无效。")
    return paths


def _set_resource_limits(arguments: argparse.Namespace) -> None:
    limits = (
        (resource.RLIMIT_AS, arguments.address_space_bytes),
        (resource.RLIMIT_CPU, arguments.cpu_seconds),
        (resource.RLIMIT_FSIZE, arguments.file_size_bytes),
        (resource.RLIMIT_NOFILE, 128),
        (resource.RLIMIT_NPROC, 64),
        (resource.RLIMIT_CORE, 0),
    )
    for resource_id, maximum in limits:
        resource.setrlimit(resource_id, (maximum, maximum))


def _restrict_filesystem(paths: dict[str, Path]) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(
        _LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(),
        0,
        _LANDLOCK_CREATE_RULESET_VERSION,
    )
    if abi < 1:
        _raise_errno("Landlock 不可用")
    handled = _WRITE_ACCESS_V1
    if abi >= _LANDLOCK_ABI_REFER:
        handled |= _ACCESS_REFER
    if abi >= _LANDLOCK_ABI_TRUNCATE:
        handled |= _ACCESS_TRUNCATE
    ruleset_attr = _LandlockRulesetAttr(handled_access_fs=handled)
    ruleset_fd = libc.syscall(
        _LANDLOCK_CREATE_RULESET,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    if ruleset_fd < 0:
        _raise_errno("无法创建 Landlock ruleset")
    try:
        for path in _existing_system_paths():
            _add_path_rule(libc, ruleset_fd, path, _READ_ONLY_ACCESS)
        for path in (Path("/dev/null"), Path("/dev/urandom")):
            if path.exists():
                _add_path_rule(
                    libc,
                    ruleset_fd,
                    path,
                    _ACCESS_READ_FILE | _ACCESS_WRITE_FILE,
                )
        _add_path_rule(
            libc,
            ruleset_fd,
            paths["input"],
            _ACCESS_READ_FILE,
        )
        write_access = handled & (
            _WRITE_ACCESS_V1 | _ACCESS_REFER | _ACCESS_TRUNCATE
        )
        for name in ("output", "profile", "temporary"):
            _add_path_rule(libc, ruleset_fd, paths[name], write_access)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            _raise_errno("无法设置 no_new_privs")
        if libc.syscall(_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) != 0:
            _raise_errno("无法启用 Landlock")
    finally:
        os.close(ruleset_fd)


def _existing_system_paths() -> tuple[Path, ...]:
    candidates = (
        Path("/usr"),
        Path("/lib"),
        Path("/lib64"),
        Path("/etc/fonts"),
        Path("/etc/ld.so.cache"),
        Path("/etc/locale.alias"),
        Path("/etc/locale.conf"),
        Path("/etc/localtime"),
        Path("/proc/self"),
        Path("/sys/devices/system/cpu"),
    )
    return tuple(path for path in candidates if path.exists())


def _add_path_rule(
    libc: ctypes.CDLL,
    ruleset_fd: int,
    path: Path,
    access: int,
) -> None:
    path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        attribute = _LandlockPathBeneathAttr(
            allowed_access=access,
            parent_fd=path_fd,
            reserved=0,
        )
        if (
            libc.syscall(
                _LANDLOCK_ADD_RULE,
                ruleset_fd,
                _LANDLOCK_RULE_PATH_BENEATH,
                ctypes.byref(attribute),
                0,
            )
            != 0
        ):
            _raise_errno("无法添加 Landlock 路径规则")
    finally:
        os.close(path_fd)


def _restrict_syscalls() -> None:
    try:
        library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError as error:
        raise OSError(errno.ENOSYS, "libseccomp 不可用") from error
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(_SECCOMP_ACTION_ALLOW)
    if not context:
        raise OSError(errno.ENOMEM, "无法创建 seccomp filter")
    try:
        denied_action = _SECCOMP_ACTION_ERRNO | errno.EPERM
        for name in _DENIED_SYSCALLS:
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:
                continue
            if library.seccomp_rule_add(context, denied_action, number, 0) != 0:
                raise OSError(errno.EINVAL, "无法添加 seccomp 规则")
        if library.seccomp_load(context) != 0:
            raise OSError(errno.EPERM, "无法启用 seccomp")
    finally:
        library.seccomp_release(context)


def _raise_errno(message: str) -> None:
    error_number = ctypes.get_errno() or errno.EPERM
    raise OSError(error_number, message)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError):
        sys.exit(_SANDBOX_UNAVAILABLE)
