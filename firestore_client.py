"""Shared Firestore client for project-harrison.

Uses Application Default Credentials (run `gcloud auth application-default
login` once, or when credentials expire) — no service-account JSON or API
key is stored in this project.
"""
from __future__ import annotations

from google.cloud import firestore

PROJECT_ID = "project-harrison"

_client: firestore.Client | None = None


def get_client() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client(project=PROJECT_ID)
    return _client
