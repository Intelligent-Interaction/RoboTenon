"""Optional local Grafana startup, invoked only by the CLI."""
import platform
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

DEFAULT_GRAFANA_URL = (
    "http://127.0.0.1:3000/d/robotenon-overview/robotenon-overview"
    # Do not enable Grafana kiosk mode here.  Kiosk mode hides the main menu
    # and dashboard toolbar, which also hides the New/Edit controls and the
    # template-variable (dimension) selectors.  The Portal already provides
    # a compact embedded view; users can still switch to full-screen in
    # Grafana when they want a read-only wallboard.
    "?orgId=1&refresh=5s"
)


def _grafana_is_local(url: str) -> bool:
    """Return whether *url* points at the local development Grafana.

    Automatic startup is intentionally restricted to loopback URLs. A custom
    remote Grafana endpoint must never cause the Portal to invoke Docker.
    """
    try:
        parsed = urlsplit(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    except ValueError:
        return False


def _grafana_health(url: str, *, timeout: float = 0.8) -> bool:
    try:
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return False
        health_url = f"{parsed.scheme}://{parsed.netloc}/api/health"
        # Bypass HTTP(S)_PROXY for loopback. This also makes startup reliable
        # on machines that export a proxy for normal internet traffic.
        opener = build_opener(ProxyHandler({}))
        with opener.open(Request(health_url, headers={"User-Agent": "RoboTenon/0.1"}), timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, URLError, ValueError):
        return False


def _compose_command() -> list[str] | None:
    # Docker Desktop normally puts ``docker`` on PATH, but macOS app installs
    # can leave the CLI plugin outside a shell's PATH. Check the conventional
    # locations as well so the default auto-start remains zero-configuration.
    docker_candidates = [
        shutil.which("docker"),
        "/Applications/Docker.app/Contents/Resources/bin/docker",
        "/usr/local/bin/docker",
        "/opt/homebrew/bin/docker",
    ]
    for candidate in docker_candidates:
        if candidate and Path(candidate).is_file():
            return [str(candidate), "compose"]
    compose_candidates = [
        shutil.which("docker-compose"),
        str(Path.home() / ".docker/cli-plugins/docker-compose"),
        "/usr/local/lib/docker/cli-plugins/docker-compose",
        "/usr/libexec/docker/cli-plugins/docker-compose",
        "/usr/lib/docker/cli-plugins/docker-compose",
        "/opt/homebrew/lib/docker/cli-plugins/docker-compose",
    ]
    for candidate in compose_candidates:
        if candidate and Path(candidate).is_file():
            return [str(candidate)]
    return None


def _compose_root() -> Path | None:
    candidates = (Path(__file__).resolve().parents[2], Path.cwd())
    for candidate in candidates:
        if (candidate / "docker-compose.yml").is_file():
            return candidate
    return None


def _docker_daemon_ready(command: list[str], *, timeout: float = 5.0) -> tuple[bool, str]:
    """Check the Docker daemon before starting a potentially long image pull."""
    binary = command[0] if command else "docker"
    if Path(binary).name == "docker":
        probe = [binary, "info"]
    else:
        # Legacy docker-compose has no equivalent lightweight daemon probe;
        # ``version`` still catches a broken executable early.
        probe = [binary, "version"]
    try:
        result = subprocess.run(probe, check=False, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stdout or "").strip().replace("\n", " ")[-500:]
    return True, ""


def ensure_grafana(url: str, *, auto_start: bool = True, timeout_s: float = 180.0) -> bool:
    """Ensure the local Compose Grafana is reachable, if requested.

    This is called by the Web Portal CLI, not by ``build_server``; importing
    RoboTenon or embedding the handler therefore never starts external services.
    The function is best-effort: a missing Docker installation leaves the
    Portal usable with its local WAL-backed Metrics and Trace views.
    """
    if not _grafana_is_local(url):
        return _grafana_health(url)
    if _grafana_health(url):
        return True
    if not auto_start:
        print("Grafana is not reachable at %s; automatic startup disabled." % url)
        return False
    root = _compose_root()
    command = _compose_command() if root else None
    if not root or not command:
        if platform.system() == "Linux":
            hint = "install Docker Engine with the Compose plugin"
        elif platform.system() == "Darwin":
            hint = "install/start Docker Desktop (for example `brew install --cask docker` then `open -a Docker`)"
        else:
            hint = "install a Docker Engine and Compose implementation"
        print("Grafana is not reachable at %s; Docker Compose was not found; %s. "
              "Restart this command afterwards (or set --no-auto-grafana)." % (url, hint))
        return False
    daemon_ready, daemon_error = _docker_daemon_ready(command)
    if not daemon_ready and Path(command[0]).name == "docker" and platform.system() == "Darwin":
        # Docker Desktop exposes this command on recent macOS installs. Linux
        # uses a normal Docker Engine service and must not invoke this command.
        try:
            subprocess.run([command[0], "desktop", "start"], check=False,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            pass
        startup_deadline = time.monotonic() + 45.0
        while time.monotonic() < startup_deadline:
            daemon_ready, daemon_error = _docker_daemon_ready(command, timeout=3.0)
            if daemon_ready:
                break
            time.sleep(1.0)
    if not daemon_ready:
        if platform.system() == "Linux":
            hint = "start the Docker Engine (for example: sudo systemctl start docker) and retry"
        elif platform.system() == "Darwin":
            hint = "start Docker Desktop and retry"
        else:
            hint = "start the Docker daemon and retry"
        print("Docker daemon is not ready; %s. %s" % (hint, daemon_error))
        return False
    if not os.getenv("GRAFANA_ADMIN_PASSWORD"):
        try:
            fd = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as stream:
                stream.write("GRAFANA_ADMIN_PASSWORD=" + secrets.token_urlsafe(32) + "\n")
    compose = command + ["up", "-d", "grafana", "prometheus", "metricsd"]
    print("Starting Grafana Compose; image pull/build progress follows…", flush=True)
    try:
        # Inherit the terminal so Compose can render its normal pull/build
        # progress bar. The detached containers keep running after Portal
        # starts; no hidden pipe can fill up and stall the child process.
        process = subprocess.Popen(compose, cwd=root, start_new_session=True)
    except OSError as exc:
        print("Grafana auto-start failed: %s" % exc)
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _grafana_health(url, timeout=1.0):
            print("Grafana ready: %s" % url)
            return True
        return_code = process.poll()
        if return_code is not None and return_code != 0:
            print("Grafana auto-start failed (exit %d). Run `docker compose logs grafana` for details." % return_code)
            return False
        time.sleep(0.5)
    if process.poll() is None:
        print("Grafana image pull/start is still running in the background; health check timed out after %.0f s. "
              "Run `docker compose ps` and refresh the Portal later." % timeout_s)
    else:
        print("Grafana containers started but health check timed out: %s" % url)
    return False

