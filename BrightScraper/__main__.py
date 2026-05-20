"""Module entry point for running BrightScraper as a package."""

import os

import uvicorn

from .api import app


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "5000")), reload=False)
