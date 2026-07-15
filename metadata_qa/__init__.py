"""Kubernetes job management web application."""

from metadata_qa.application import create_app
from metadata_qa.config import Settings

__all__ = ["Settings", "create_app"]
