"""Container health check: GET the app's /health/ endpoint on the loopback interface. Exit status 0 = healthy.

A script instead of an inline `python -c "..."` because Quadlet re-quotes inline commands and mangles them.
Proxy settings from the environment are ignored on purpose (this must never leave the container).
"""
import sys
import urllib.request

DEFAULT_URL = 'http://127.0.0.1:8000/health/'


def check(url: str = DEFAULT_URL, timeout: float = 3) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:  # connection refused, timeout, HTTP error: all mean "not healthy"
        return False


if __name__ == '__main__':
    sys.exit(0 if check(*sys.argv[1:2]) else 1)
