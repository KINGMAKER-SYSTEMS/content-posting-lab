"""Canonical version binding for the joined Dossier ingredient authorities."""

import hashlib


def dossier_catalog_version(*authority_hashes: str) -> str:
    canonical = "\n".join(authority_hashes).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()
