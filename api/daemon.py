"""Frozen desktop sidecar entry point."""

import os

import uvicorn

# Frozen desktop builds must not initialize development reload/watch hooks.
os.environ.setdefault("NODE_ENV", "production")

from api.main import app  # noqa: E402


def main() -> None:
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8001")),
        reload=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
