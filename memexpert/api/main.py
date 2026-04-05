"""Console entry point for running the MemeXpert API."""

from __future__ import annotations

from memexpert.core.config import get_settings


def main() -> None:
    """Run the FastAPI application through uvicorn's app factory mode."""

    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "memexpert.api.app:create_app",
        factory=True,
        host=settings.app_host,
        port=settings.app_port,
    )


__all__ = ["main"]
