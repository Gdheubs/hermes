"""Render entrypoint for the isolated Lin Hermes Runtime service."""

import os

import uvicorn

from lin_runtime.app import app


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
