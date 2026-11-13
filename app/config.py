"""运行参数（环境变量覆盖）。"""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


DB_PATH = os.environ.get("DB_PATH", "/data/worker.db")
HEADLESS = os.environ.get("HEADLESS", "true").strip().lower() in ("1", "true", "yes", "on")

NAV_TIMEOUT_MS = _int("NAV_TIMEOUT_MS", 15000)
SUBMIT_TIMEOUT_MS = _int("SUBMIT_TIMEOUT_MS", 8000)
VISIBILITY_WAIT_MS = _int("VISIBILITY_WAIT_MS", 1500)
VISIBILITY_MAX_ATTEMPTS = _int("VISIBILITY_MAX_ATTEMPTS", 3)

WORKER_IDLE_SLEEP_S = _float("WORKER_IDLE_SLEEP_S", 0.5)
BROWSER_IDLE_CLOSE_S = _int("BROWSER_IDLE_CLOSE_S", 30)
MAX_RUNTIME_ERRORS = _int("MAX_RUNTIME_ERRORS", 5)
RETRY_BACKOFF_S = _float("RETRY_BACKOFF_S", 0.4)
