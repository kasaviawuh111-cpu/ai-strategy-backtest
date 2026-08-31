"""ASGI entrypoint used by Uvicorn and container deployments."""

from fastapi import FastAPI

from ashare_lab.bootstrap import create_configured_app


def create_app() -> FastAPI:
    return create_configured_app()
