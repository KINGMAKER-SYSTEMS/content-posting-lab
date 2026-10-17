"""Explicit, operator-only per-job signed URL token rotation.

Run from a checkout with ``python -m scripts.rotate_job_download_token``.
The production image copies services/ but not scripts/, so use
``python -m services.job_token_rotation`` there. No store path is inferred;
dry-run first and supply its revision to --apply.
"""

from services.job_token_rotation import main


if __name__ == "__main__":
    raise SystemExit(main())
