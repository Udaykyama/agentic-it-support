"""Probe the running API without importing application code or loading AI clients."""

import os
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlsplit


def main():
    connection = None
    try:
        public_url = urlsplit(os.environ.get("PUBLIC_URL", "http://localhost:8000"))
        if (
            public_url.scheme not in {"http", "https"} or not public_url.hostname
            or public_url.username or public_url.password
        ):
            return 1
        connection = HTTPConnection("127.0.0.1", 8000, timeout=3)
        connection.request(
            "GET", "/health/ready",
            headers={"Host": public_url.netloc, "Connection": "close"},
        )
        response = connection.getresponse()
        return 0 if response.status == 200 else 1
    except (HTTPException, OSError, ValueError):
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
