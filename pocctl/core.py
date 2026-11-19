"""Configuration, console output, subprocess execution and the lifecycle lock shared by every command."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from collections.abc import Callable, Iterable, Iterator
from typing import Any, NamedTuple, NoReturn, TypeVar

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT.parent
OWNER = "amirasyraf"
DOMAIN = "idp.amirasyraf.dev"
BUILDER_REPO = f"{OWNER}/vendy-builder"
PLATFORM_REPO = f"{OWNER}/vendy-platform"
PLATFORM_DIR = PROJECTS / "vendy-platform"
IDP_DIR = PROJECTS / "vendy-idp"
CF_DNS_DIR = Path.home() / "projects/cloudflare/amirasyraf.dev"
INFRA_DIR = ROOT / "infra"
IMAGES_DIR = INFRA_DIR / "images"  # SSH CA + Azure gallery; survives down --keep-images
GEN = ROOT / ".generated"
LOGS = GEN / "logs"
CA_FINGERPRINT_RE = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
HEALTH_URL = f"https://{DOMAIN}/.backstage/health/v1/readiness"
USERS_FILE = ROOT / ".poc-users"  # username:scrypt-hash lines, gitignored, persists across cycles
PASSWORDS_FILE = ROOT / ".poc-passwords"  # username:plaintext, gitignored; POC test logins
POC_ENV = ROOT / ".poc.env"
AZURE_READER_PEM = ROOT / ".poc-azure-reader.pem"
RUN_CANCEL_TIMEOUT = 900
CLEAN_RETRY_SECONDS = 30
CLEAN_MAX_ATTEMPTS = 6
LOCK_FILE = Path.home() / ".cache/compute-factory/lifecycle.lock"
MAINTENANCE_VARIABLE = "CF_POC_MAINTENANCE"
AWS_ACCOUNT = "134584031874"
AWS_PROFILE = os.environ.get("AWS_PROFILE") or "amirasyraf"
AWS_REGION = "ap-southeast-2"
AZURE_SUBSCRIPTION = "74b39a79-f79d-4adb-8ae0-fe9118fc564a"
AZURE_TENANT = "c26545da-80fa-4c31-9a8f-a4ae475bd17e"
AZURE_LOCATION = "australiaeast"
STATE_BUCKET = f"cf-tfstate-{AWS_ACCOUNT}-apse2"
WORKFLOWS = ((BUILDER_REPO, "bake.yml"), (PLATFORM_REPO, "stage.yml"))
CADDY_BACKUP = GEN / "caddy-data.tgz"
CADDY_VOLUME = "idp_caddy_data"
SSH_KEY = Path.home() / ".ssh/id_ed25519"
SSH_OPTS = [
    "-i", str(SSH_KEY), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
]

T = TypeVar("T")
R = TypeVar("R")


class Fatal(Exception):
    """Abort the current command with this message."""


class Unknown(Exception):
    """A lookup could not establish the state it was asked about. Never treat it as absence."""


def die(message: str) -> NoReturn:
    raise Fatal(message)


def configure_environment() -> None:
    os.environ["AWS_PROFILE"] = AWS_PROFILE
    os.environ["AWS_REGION"] = os.environ["AWS_DEFAULT_REGION"] = AWS_REGION
    os.environ["AWS_PAGER"] = ""
    os.environ["AWS_CLI_SESSION_ID_DISABLED"] = "true"
    os.environ["AWS_MAX_ATTEMPTS"] = os.environ.get("AWS_MAX_ATTEMPTS") or "3"
    os.environ["TF_IN_AUTOMATION"] = "1"
    os.environ["TF_INPUT"] = "0"


# ---------------------------------------------------------------- output
# Each thread writes to its own streams, so a background job can log to a file (like `>log 2>&1`)
# while the main thread keeps the terminal. Subprocesses inherit the calling thread's streams.

_local = threading.local()
_print_lock = threading.Lock()


def _out() -> Any:
    return getattr(_local, "out", None) or sys.stdout


def _err() -> Any:
    return getattr(_local, "err", None) or sys.stderr


def _write(stream: Any, text: str) -> None:
    with _print_lock:
        stream.write(text)
        stream.flush()


def log(message: str) -> None:
    _write(_out(), f"\n\033[1m==> {message}\033[0m\n")


def info(message: str) -> None:
    _write(_out(), f"    {message}\n")


def echo(message: str) -> None:
    _write(_out(), f"{message}\n")


def warn(message: str) -> None:
    _write(_err(), f"    \033[33mWARN: {message}\033[0m\n")


def error(message: str) -> None:
    _write(sys.stderr, f"\033[31mERROR: {message}\033[0m\n")


def success(message: str) -> None:
    _write(sys.stdout, f"\n\033[32m{message}\033[0m\n")


def show_tail(path: Path, lines: int | None = None) -> None:
    try:
        text = path.read_text(errors="replace").splitlines()
    except OSError:
        return
    if lines is not None:
        text = text[-lines:]
    _write(_err(), "".join(line + "\n" for line in text))


def last_line(text: str) -> str:
    rows = [row for row in text.strip().splitlines() if row.strip()]
    return rows[-1] if rows else "no diagnostics"


@contextlib.contextmanager
def output_to(path: Path, append: bool = False) -> Iterator[None]:
    previous = (getattr(_local, "out", None), getattr(_local, "err", None))
    with open(path, "a" if append else "w") as stream:
        _local.out = _local.err = stream
        try:
            yield
        finally:
            _local.out, _local.err = previous


# ---------------------------------------------------------------- concurrency


def _bound(fn: Callable[[], R]) -> Callable[[], R]:
    out, err = getattr(_local, "out", None), getattr(_local, "err", None)

    def call() -> R:
        _local.out, _local.err = out, err
        return fn()

    return call


def spawn(fn: Callable[[], R], log_path: Path | None = None) -> Future[R]:
    """Run fn in its own thread with the caller's output streams, or with log_path as both."""

    def call() -> R:
        if log_path is None:
            return fn()
        with output_to(log_path):
            return fn()

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(_bound(call))
    finally:
        pool.shutdown(wait=False)


def parallel(fn: Callable[[T], R], items: Iterable[T], workers: int = 8) -> list[R]:
    """Map fn over items concurrently, keeping order. Exceptions propagate."""
    items = list(items)
    if not items:
        return []
    def task(item: T) -> Callable[[], R]:
        return _bound(lambda: fn(item))

    pool = ThreadPoolExecutor(max_workers=min(workers, len(items)))
    try:
        futures = [pool.submit(task(item)) for item in items]
        return [future.result() for future in futures]
    except BaseException:
        # Stop the other workers before joining them, so they cannot start further steps.
        cancel_background()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


# Set when the command is failing or exiting: background threads start no new subprocesses and
# their waits return at once. The main thread keeps working (for example to restore gates).
SHUTDOWN = threading.Event()


def _in_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def pause(seconds: float) -> bool:
    """Sleep; True when a background thread should give up. The main thread sleeps normally (Ctrl-C
    interrupts it directly)."""
    if _in_main_thread():
        time.sleep(seconds)
        return False
    return SHUTDOWN.wait(seconds)


def cancel_background() -> None:
    """Stop background work: no new subprocesses in worker threads, and every running child process
    group (including grandchildren such as yarn, docker or terraform providers) is terminated."""
    terminate_children()  # sets SHUTDOWN under the start/registration lock


def wait_seconds(seconds: float, stop: threading.Event | None) -> bool:
    """Sleep; True when stop was set or the command is exiting."""
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        if SHUTDOWN.is_set() or (stop is not None and stop.is_set()):
            return True
        (stop or SHUTDOWN).wait(min(remaining, 1))
    return SHUTDOWN.is_set() or (stop is not None and stop.is_set())


# ---------------------------------------------------------------- processes

_lock_fd: int | None = None
_children: set[subprocess.Popen[str]] = set()
_children_lock = threading.Lock()


class Result(NamedTuple):
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


def run(
    cmd: list[str],
    *,
    capture: bool = True,
    input: str | None = None,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
    log: Path | None = None,
    append: bool = False,
    env: dict[str, str | None] | None = None,
    cwd: Path | None = None,
    timeout: float | None = None,
    group: ProcessGroup | None = None,
) -> Result:
    """Run a command. Captured output is returned; with capture=False it goes to this thread's
    streams, and with log= both streams go to that file. Stdin is /dev/null unless given.
    The child gets its own process group; if this call is interrupted, that group is stopped and
    reaped before the exception propagates."""
    log_file = open(log, "a" if append else "w") if log else None
    try:
        if log_file:
            stdout = stdout or log_file
            stderr = stderr or log_file
        if not capture:
            out, err = _out(), _err()
            out.flush()
            err.flush()
            stdout = stdout or out
            stderr = stderr or err
        full_env = None
        if env:
            full_env = dict(os.environ)
            for key, value in env.items():
                if value is None:
                    full_env.pop(key, None)
                else:
                    full_env[key] = value
        # Starting and registering happen under the same lock that cancellation takes, so a process
        # either is refused or is visible to the canceller.
        with _children_lock:
            if (SHUTDOWN.is_set() and not _in_main_thread()) or (group is not None and group.cancelled):
                return Result(130, "", f"{cmd[0]}: not started; background work was cancelled")
            try:
                proc = subprocess.Popen(
                    [str(part) for part in cmd],
                    stdin=subprocess.PIPE if input is not None else (stdin if stdin is not None else subprocess.DEVNULL),
                    stdout=stdout if stdout is not None else subprocess.PIPE,
                    stderr=stderr if stderr is not None else subprocess.PIPE,
                    cwd=cwd,
                    env=full_env,
                    # Children keep the lifecycle lock until they exit, like the workers they may start.
                    pass_fds=(_lock_fd,) if _lock_fd is not None else (),
                    # Its own process group, so cancellation reaches everything it starts.
                    start_new_session=True,
                    text=True,
                    errors="replace",
                )
            except OSError as exc:
                return Result(127, "", f"{cmd[0]}: {exc}")
            _children.add(proc)
            if group is not None:
                group.procs.append(proc)
        try:
            try:
                out_text, err_text = proc.communicate(input, timeout=timeout)
            except subprocess.TimeoutExpired:
                _signal_group(proc, signal.SIGKILL)
                out_text, err_text = proc.communicate()
                return Result(124, out_text or "", f"{err_text or ''}\n{cmd[0]} timed out after {timeout}s")
            except BaseException:
                # Ctrl-C reaches only ./poc (the child has its own session): stop the child's group here.
                _stop_group(proc)
                raise
        finally:
            with _children_lock:
                _children.discard(proc)
        return Result(proc.returncode, out_text or "", err_text or "")
    finally:
        if log_file:
            log_file.close()


class ProcessGroup:
    """Processes started by run(group=...) that can be cancelled together, including ones not yet started."""

    def __init__(self) -> None:
        self.cancelled = False
        self.procs: list[subprocess.Popen[str]] = []

    def cancel(self) -> None:
        with _children_lock:
            self.cancelled = True
            procs = list(self.procs)
        _stop_groups(procs)


def _signal_group(proc: subprocess.Popen[str], sig: int) -> None:
    # Signal the group even if its leader has exited: background descendants keep the group alive,
    # and a registered child's group ID cannot be reused while any member remains.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


def _group_alive(proc: subprocess.Popen[str]) -> bool:
    proc.poll()  # reap the leader, so a zombie leader does not count as a live member
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_groups(procs: list[subprocess.Popen[str]], grace: float = 10) -> None:
    """SIGTERM each group, wait until every member of every group has exited, and SIGKILL the
    groups still alive after the grace period (or on a second interrupt)."""
    for proc in procs:
        _signal_group(proc, signal.SIGTERM)
    deadline = time.monotonic() + grace
    try:
        while any(_group_alive(p) for p in procs) and time.monotonic() < deadline:
            time.sleep(0.2)
    except BaseException:
        pass
    for proc in procs:
        if _group_alive(proc):
            _signal_group(proc, signal.SIGKILL)
    for proc in procs:
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def _stop_group(proc: subprocess.Popen[str]) -> None:
    _stop_groups([proc])


def terminate_children() -> None:
    with _children_lock:
        SHUTDOWN.set()
        children = list(_children)
    _stop_groups(children)


def require(result: Result, what: str) -> str:
    if not result.ok:
        die(f"{what}: {last_line(result.err)}")
    return result.out


def retry_read(label: str, cmd: list[str]) -> Result:
    """Only for reads. A failed read never supplies partial output; never use it for writes."""
    result = Result(1, "", "")
    for attempt in range(1, 5):
        result = run(cmd)
        if result.ok:
            return result
        warn(f"{label}: read failed ({attempt}/4): {last_line(result.err)}")
        if attempt < 4 and pause(2**attempt):
            break
    return result


def gh_read(*args: str) -> Result:
    return retry_read(f"GitHub {args[0]}", ["gh", *args])


def parse_json(text: str, what: str) -> Any:
    if not text.strip():
        raise Unknown(f"{what}: empty response")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise Unknown(f"{what}: invalid JSON ({exc})") from None


def aws_cmd(*args: str) -> list[str]:
    return ["aws", "--cli-connect-timeout", "10", "--cli-read-timeout", "20", *args]


def aws(*args: str) -> Result:
    return run(aws_cmd(*args))


def aws_json(*args: str, allow_empty: bool = False) -> Any:
    result = aws(*args, "--output", "json")
    if not result.ok:
        raise Unknown(f"aws {' '.join(args[:2])}: {last_line(result.err)}")
    if allow_empty and not result.out.strip():
        return None
    return parse_json(result.out, f"aws {' '.join(args[:2])}")


def aws_ids(*args: str) -> list[str]:
    data = aws_json(*args)
    if not isinstance(data, list):
        raise Unknown(f"aws {' '.join(args[:2])}: expected a list")
    return [str(item) for item in data if item is not None]


def aws_error(text: str, code: str) -> bool:
    return f"An error occurred ({code}) when calling the " in text


def delete_aws(label: str, not_found: str, *args: str) -> bool:
    result = aws(*args)
    if result.ok or aws_error(result.err, not_found):
        info(f"removed {label}")
        return True
    warn(f"could not remove {label}: {last_line(result.err)}")
    return False


def az_cmd(*args: str) -> list[str]:
    return ["az", *args, "--subscription", AZURE_SUBSCRIPTION, "--only-show-errors"]


def az(*args: str) -> Result:
    return run(az_cmd(*args))


def az_json(*args: str) -> Any:
    result = az(*args, "-o", "json")
    if not result.ok:
        raise Unknown(f"az {' '.join(args[:3])}: {last_line(result.err)}")
    return parse_json(result.out, f"az {' '.join(args[:3])}")


def ssh(ip: str, remote: str, **kwargs: Any) -> Result:
    return run(["ssh", *SSH_OPTS, f"ubuntu@{ip}", remote], **kwargs)


# ---------------------------------------------------------------- terraform


def tf(root: Path, *args: str, **kwargs: Any) -> Result:
    env = {"TF_DATA_DIR": str(root / ".terraform"), **kwargs.pop("env", {})}
    return run(["terraform", f"-chdir={root}", *args], env=env, **kwargs)


def tf_output(root: Path, name: str) -> str | None:
    result = tf(root, "output", "-raw", name)
    return result.out if result.ok and result.out else None


def infra_out(name: str) -> str:
    return tf_output(INFRA_DIR, name) or ""


def root_state(root: Path) -> list[str]:
    state = root / "terraform.tfstate"
    if state.is_symlink():
        raise Unknown(f"refusing a symlinked local Terraform state in {root}")
    if not state.exists():
        return []
    result = tf(root, "state", "list")
    if not result.ok:
        raise Unknown(f"terraform state list in {root}: {last_line(result.err)}")
    return [line for line in result.out.splitlines() if line.strip()]


def _tf_vars(*extra: str) -> list[str]:
    pairs = [
        f"aws_profile={AWS_PROFILE}", f"region={AWS_REGION}", f"azure_subscription_id={AZURE_SUBSCRIPTION}",
        f"azure_tenant_id={AZURE_TENANT}", f"azure_location={AZURE_LOCATION}", *extra,
    ]
    return [arg for pair in pairs for arg in ("-var", pair)]


def infra_apply_or_destroy(action: str, **kwargs: Any) -> Result:
    return tf(INFRA_DIR, action, "-auto-approve", "-input=false", *_tf_vars(
        "azure_reader_cert_path=../.poc-azure-reader.pem",
        f"admin_ssh_public_key_path={Path.home()}/.ssh/id_ed25519.pub",
    ), **kwargs)


def images_apply_or_destroy(action: str, **kwargs: Any) -> Result:
    return tf(IMAGES_DIR, action, "-auto-approve", "-input=false", *_tf_vars(), **kwargs)


# Before the image tier had its own root, infra/ held the CA and gallery. Applying the current
# infra/ to such a state would destroy the CA, so refuse; this reads the file and needs no init.
LEGACY_RE = re.compile(r"^(tls_private_key|aws_secretsmanager_secret(_version)?\.ssh_ca$|aws_ssm_parameter\.ssh_ca_pub$|azurerm_)")


def require_split_layout() -> None:
    state = INFRA_DIR / "terraform.tfstate"
    if not state.exists():
        return
    try:
        resources = json.loads(state.read_text()).get("resources") or []
    except (OSError, ValueError, AttributeError):
        die(f"cannot read {state}")
    legacy = [
        f"{r.get('type')}.{r.get('name')}" for r in resources
        if r.get("mode") == "managed" and LEGACY_RE.match(f"{r.get('type')}.{r.get('name')}")
    ]
    if legacy:
        die(f"infra state still holds image-tier resources ({' '.join(legacy)}); move them to infra/images "
            "with terraform state mv first (DESIGN.md §12.1)")


# ---------------------------------------------------------------- preflight


def require_tools(*extra: str) -> None:
    for tool in ("terraform", "gh", "aws", "az", "ssh", "git", "curl", *extra):
        if shutil.which(tool) is None:
            die(f"missing tool: {tool}")
    account = aws("sts", "get-caller-identity", "--query", "Account", "--output", "text")
    if not account.ok:
        die(f"AWS profile {AWS_PROFILE} not usable (aws sso login?)")
    if account.out.strip() != AWS_ACCOUNT:
        die(f"AWS profile {AWS_PROFILE} points at {account.out.strip()}, expected {AWS_ACCOUNT}")
    if not gh_read("auth", "status").ok:
        die("cannot verify GitHub authentication; check gh auth status")
    # The sweeps use the az CLI. Terraform's azurerm provider needs ARM_* only for `up` and for
    # destroying an existing local infra state; see DESIGN.md §9.
    if run(["az", "account", "show", "--query", "id", "-o", "tsv"]).out.strip() != AZURE_SUBSCRIPTION:
        die(f"az CLI not logged in to subscription {AZURE_SUBSCRIPTION} (run azsp)")


def require_terraform_azure_auth() -> bool:
    if (os.environ.get("ARM_TENANT_ID") or AZURE_TENANT) != AZURE_TENANT:
        warn("ARM_TENANT_ID is not the POC tenant")
        return False
    if (os.environ.get("ARM_SUBSCRIPTION_ID") or AZURE_SUBSCRIPTION) != AZURE_SUBSCRIPTION:
        warn("ARM_SUBSCRIPTION_ID is not the POC subscription")
        return False
    os.environ["ARM_TENANT_ID"] = AZURE_TENANT
    os.environ["ARM_SUBSCRIPTION_ID"] = AZURE_SUBSCRIPTION
    os.environ["ARM_CLIENT_ID"] = os.environ.get("ARM_CLIENT_ID") or "b6a2750b-5b51-487a-8903-d01c0a00a4e2"
    os.environ["ARM_CLIENT_CERTIFICATE_PATH"] = (
        os.environ.get("ARM_CLIENT_CERTIFICATE_PATH") or str(Path.home() / ".config/azure/cf-admin-local.pfx")
    )
    os.environ["ARM_CLIENT_CERTIFICATE_PASSWORD"] = os.environ.get("ARM_CLIENT_CERTIFICATE_PASSWORD", "")
    if not os.access(os.environ["ARM_CLIENT_CERTIFICATE_PATH"], os.R_OK):
        warn(f"Terraform Azure certificate not readable: {os.environ['ARM_CLIENT_CERTIFICATE_PATH']}")
        return False
    return True


def require_image_tools(*extra: str) -> None:
    require_tools("ssh-keygen", *extra)
    try:
        import yaml  # noqa: F401
    except ImportError:
        die("python3 needs PyYAML (pip install pyyaml) to read images.yaml")
    if not require_terraform_azure_auth():
        die("Terraform Azure authentication is required for the image tier")
    require_split_layout()


# ---------------------------------------------------------------- lifecycle lock


def acquire_lock() -> None:
    """One lifecycle mutation at a time per user, across checkouts (CONTRACTS.md keeps this path)."""
    global _lock_fd
    if _lock_fd is not None:
        return
    LOCK_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        die("POC lifecycle lock is held; another command or its workers are still running")
    _lock_fd = fd


def prepare_lifecycle() -> None:
    # Do not let a caller silently redirect a lifecycle command to another state or append
    # -target, -lock=false, etc. to the Terraform commands.
    for name, value in os.environ.items():
        if (name.startswith("TF_CLI_ARGS") or name.startswith("TF_VAR_")) and value:
            die(f"{name} is not supported for POC lifecycle commands")
    if (os.environ.get("TF_WORKSPACE") or "default") != "default":
        die("POC lifecycle uses only the default Terraform workspace")
    os.environ.pop("TF_DATA_DIR", None)
    os.environ["TF_WORKSPACE"] = "default"
    os.umask(0o077)
    LOGS.mkdir(parents=True, exist_ok=True)
    GEN.chmod(0o700)
    LOGS.chmod(0o700)


# ---------------------------------------------------------------- small helpers


def nonempty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def tls_backup_has_cert(path: Path) -> bool:
    try:
        with tarfile.open(path, "r:gz") as archive:
            return any(name.endswith(".crt") for name in archive.getnames())
    except (OSError, tarfile.TarError):
        return False


def poc_env_github_token() -> str | None:
    """.poc.env is a shell file; read it the way a shell does."""
    result = run(["bash", "-c", 'source "$1" && printf %s "${GITHUB_TOKEN:?}"', "_", str(POC_ENV)])
    return result.out if result.ok and result.out else None
