"""Ordered generation backends: a local engine first, its hosted twin as a bounded fallback.

A job still names exactly one provider. When ``CONTENT_LAB_BACKEND_ORDER`` is
``on``, that provider is looked up in ``providers/backend_order.json`` and the
job walks the first chain that contains it, in order:

- a ``billing: local`` engine is tried only when its ComfyUI server (or its
  local worker) answers and is not busy. When it is down, busy, cannot take the
  request shape, or its render fails, the job moves to the next entry;
- a hosted (paid) entry ends the walk. Its failure ends the job exactly as
  before, so one job sends at most one paid generation request plus that
  provider's own existing retries. A hosted entry the caller did not ask for is
  reserved against the daily generation budget under the job's own id, so a
  retry of the same job never reserves twice and the zero-budget stop holds.

Self-healing, not just alerting: a down or busy local engine is skipped and
the job is served by its hosted twin; every new job probes again, so jobs
return to the local engine on their own as soon as it answers and is idle. The
hosted attempt is the failed local render's one bounded retry, idempotent per
job. Each local engine's change of state logs one warning line (down, back),
each job served by a later entry logs one ``backend-order fallback`` line, and
an ``ALERT`` fires only when every backend in a job's chain has failed.

Unset or ``off`` (the default), every job uses exactly the provider it asked
for, as before this module existed. A missing or malformed order file alerts
once and also means "each job uses its own provider".
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
from pathlib import Path
from typing import Any, Callable

import httpx

log = logging.getLogger("content_lab.backend_order")

SCHEMA = "content-lab.backend-order.v1"
DEFAULT_PATH = Path(__file__).with_name("backend_order.json")
ENV = "CONTENT_LAB_BACKEND_ORDER"
VISION_URL_ENV = "CONTENT_LAB_VISION_STUDIO_URL"

_LOCAL_DEFAULTS = {"probe_timeout_s": 5.0, "max_queue": 2, "claim_timeout_s": 600.0}
_VISION_DEFAULTS = {"timeout_s": 30.0, "min_agreement": 0.95, "min_frames": 200}

_cache: tuple[str, dict | None] | None = None
_cache_lock = threading.Lock()
# backend id -> "up" or "down:<reason>", so a state change alerts once, not per job.
_state: dict[str, str] = {}


def _alert(message: str, *args: Any) -> None:
    log.error("ALERT backend-order: " + message, *args)


def _source() -> str | None:
    raw = (os.getenv(ENV) or "").strip()
    if not raw or raw.lower() in {"off", "0", "false", "no"}:
        return None
    if raw.lower() in {"on", "1", "true", "yes", "local-first"}:
        return str(DEFAULT_PATH)
    return raw  # an operator-supplied order file


def reset() -> None:
    """Drop the cached order file and engine state (tests)."""
    global _cache
    with _cache_lock:
        _cache = None
    _state.clear()


def config() -> dict | None:
    """The validated order file, or None when ordering is off or the file fails closed."""
    global _cache
    source = _source()
    if source is None:
        return None
    with _cache_lock:
        if _cache is not None and _cache[0] == source:
            return _cache[1]
        try:
            loaded = _validate(json.loads(Path(source).read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            _alert("%s is unusable (%s); every job uses the provider it asked for", source, error)
            loaded = None
        _cache = (source, loaded)
        return loaded


def _positive(value: Any, label: str, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be a positive number")
    if integer and int(value) != value:
        raise ValueError(f"{label} must be a whole number")
    return int(value) if integer else float(value)


def _validate(data: Any) -> dict:
    from . import PROVIDERS

    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError(f"schema must be {SCHEMA}")
    local = {**_LOCAL_DEFAULTS, **(data.get("local") or {})}
    local = {
        "probe_timeout_s": _positive(local["probe_timeout_s"], "local.probe_timeout_s"),
        "max_queue": _positive(local["max_queue"], "local.max_queue", integer=True),
        "claim_timeout_s": _positive(local["claim_timeout_s"], "local.claim_timeout_s"),
    }
    chains_in = data.get("chains")
    if not isinstance(chains_in, dict):
        raise ValueError("chains must map a kind to a list of chains")
    chains: list[tuple[str, tuple[str, ...]]] = []
    for kind, kind_chains in chains_in.items():
        if not isinstance(kind_chains, list):
            raise ValueError(f"chains.{kind} must be a list")
        for chain in kind_chains:
            if not isinstance(chain, list) or not chain or len(set(chain)) != len(chain):
                raise ValueError(f"chains.{kind}: each chain is a non-empty list of distinct ids")
            seen_paid = False
            for backend in chain:
                info = PROVIDERS.get(backend)
                if not isinstance(info, dict):
                    raise ValueError(f"chains.{kind}: unknown provider {backend!r}")
                if info.get("billing") == "local":
                    if seen_paid:
                        raise ValueError(f"chains.{kind}: local {backend!r} after a paid provider never runs")
                else:
                    if seen_paid:
                        raise ValueError(f"chains.{kind}: at most one paid provider per chain")
                    seen_paid = True
            chains.append((str(kind), tuple(chain)))
    return {"local": local, "chains": chains, "vision": _validate_vision(data.get("vision"))}


def _validate_vision(section: Any) -> dict | None:
    if section is None:
        return None
    studio = (section or {}).get("studio") if isinstance(section, dict) else None
    if not isinstance(studio, dict):
        raise ValueError("vision.studio must be an object")
    cfg = {**_VISION_DEFAULTS, **studio}
    for key in ("fast_model", "hard_model"):
        if not isinstance(cfg.get(key), str) or not cfg[key].strip():
            raise ValueError(f"vision.studio.{key} must name a model")
    authority = cfg.get("authority", "hint")
    if authority not in {"hint", "verdict"}:
        raise ValueError("vision.studio.authority must be hint or verdict")
    cfg["timeout_s"] = _positive(cfg["timeout_s"], "vision.studio.timeout_s")
    if authority == "verdict" and not _agreement_passed(cfg):
        # Local vision output stays a hint until a measured agreement run passes.
        _alert("vision.studio.authority=verdict without a passing agreement record; using hint")
        authority = "hint"
    cfg["authority"] = authority
    cfg["fast_requires_resident"] = cfg.get("fast_requires_resident") is True
    cfg["hard_requires_resident"] = cfg.get("hard_requires_resident", True) is not False
    return cfg


def _agreement_passed(cfg: dict) -> bool:
    record = cfg.get("agreement")
    if not isinstance(record, dict):
        return False
    rate, frames = record.get("rate"), record.get("frames")
    return (
        isinstance(rate, (int, float)) and not isinstance(rate, bool) and math.isfinite(rate)
        and isinstance(frames, int) and not isinstance(frames, bool)
        and record.get("model") == cfg["fast_model"]
        and rate >= cfg["min_agreement"] and frames >= cfg["min_frames"]
    )


def studio_vision() -> dict | None:
    """The Studio vision settings plus its URL, or None when not configured."""
    cfg = config()
    url = (os.getenv(VISION_URL_ENV) or "").strip()
    if not cfg or not cfg.get("vision") or not url:
        return None
    return {**cfg["vision"], "url": url}


def chain_for(provider: str) -> tuple[str, ...]:
    """The backends a job asking for ``provider`` walks, in order."""
    cfg = config()
    if cfg:
        for _kind, chain in cfg["chains"]:
            if provider in chain:
                return chain
    return (provider,)


async def local_ready(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Can the configured local engine take one more job right now?

    Direct mode asks the ComfyUI server's own ``/queue`` (bounded timeout);
    worker mode asks whether a worker spoke recently and is idle. A busy
    server means "refuse, don't queue forever": the job goes to the next entry.
    """
    from . import comfyui
    from .base import API_KEYS

    cfg = config() or {"local": dict(_LOCAL_DEFAULTS)}
    limits = cfg["local"]
    target = (API_KEYS.get("comfyui") or "").strip().rstrip("/")
    if not target:
        return False, "not_configured"
    if target == comfyui.WORKER_QUEUE:
        from services import local_worker_queue

        return local_worker_queue.worker_ready(limits["max_queue"])
    try:
        resp = await client.get(f"{target}/queue", timeout=limits["probe_timeout_s"])
    except httpx.TimeoutException:
        return False, "timeout"
    except httpx.HTTPError:
        return False, "unreachable"
    if resp.status_code != 200:
        return False, f"http_{resp.status_code}"
    try:
        body = resp.json()
        depth = len(body.get("queue_running") or []) + len(body.get("queue_pending") or [])
    except (ValueError, AttributeError, TypeError):
        return False, "bad_queue_reply"
    depth += comfyui.waiting_jobs(target)
    if depth >= limits["max_queue"]:
        return False, "busy"
    return True, "ok"


def _mark(backend: str, ready: bool, reason: str) -> None:
    now = "up" if ready else f"down:{reason}"
    previous = _state.get(backend)
    _state[backend] = now
    if previous == now:
        return
    if not ready:
        log.warning("backend-order: local engine %s unavailable (%s); serving from the hosted twin",
                    backend, reason)
    elif previous is not None:
        log.warning("backend-order: local engine %s is back", backend)


def _failure_reason(error: BaseException) -> str:
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.HTTPError):
        return "unreachable"
    text = str(error).lower()
    if "timed out" in text:
        return "timeout"
    if "no local worker" in text or "offline" in text or "not claimed" in text:
        return "worker_unavailable"
    return "render_failed"


async def _reserve(params: dict, backend: str, cost_usd: float) -> None:
    """Reserve a hosted fallback the caller did not reserve, once per job."""
    from services import generation_budget

    job_id = params.get("job_id")
    if not job_id or cost_usd <= 0:
        raise RuntimeError("generation_pricing_unavailable")
    if not await asyncio.to_thread(
        generation_budget.debit_generation_spend_at,
        generation_budget.jobs_store_path(), cost_usd, f"{job_id}:fallback:{backend}",
    ):
        raise RuntimeError("generation_daily_budget_reached")


async def generate(
    provider: str,
    prompt: str,
    params: dict,
    client: httpx.AsyncClient,
    *,
    price: Callable[[str], float],
) -> tuple[Any, str]:
    """Run one generation on the first backend that serves it.

    Returns ``(media, served_provider)``. ``price(provider_id)`` prices a
    substitute hosted backend from the same request.
    """
    from . import PROVIDERS

    entry = params["entry"]
    chain = chain_for(provider)
    checkpoint = entry.get("_prediction_checkpoint")
    if getattr(checkpoint, "record", None):
        # Resuming a hosted prediction never detours through a local engine.
        chain = (provider,)
    if len(chain) == 1:
        return await PROVIDERS[provider]["module"].generate(prompt, params, client), provider

    limits = (config() or {"local": dict(_LOCAL_DEFAULTS)})["local"]
    attempts: list[dict] = []
    for position, backend in enumerate(chain):
        info = PROVIDERS[backend]
        local = info.get("billing") == "local"
        final = position == len(chain) - 1
        substitute = backend != provider
        if local and not final:
            ready, reason = await local_ready(client)
            if not ready:
                attempts.append({"provider": backend, "outcome": "skipped", "reason": reason})
                _mark(backend, False, reason)
                continue
        backend_params = dict(params)
        if substitute:
            backend_params.update(model_id=info["models"][0], variant=info.get("variant"))
            if local:
                backend_params["cost_usd"] = 0.0
            else:
                cost = price(backend)
                backend_params["cost_usd"] = cost
                await _reserve(backend_params, backend, cost)
        if local and not final:
            backend_params["local_claim_timeout"] = limits["claim_timeout_s"]
        try:
            media = await info["module"].generate(prompt, backend_params, client)
        except ValueError:
            if local and substitute and not final:
                # This engine cannot take the request's shape (size, image).
                attempts.append({"provider": backend, "outcome": "skipped", "reason": "request_shape"})
                continue
            raise
        except Exception as error:  # noqa: BLE001 - only a local failure falls forward
            if not local or final:
                if attempts:
                    _alert("job=%s every backend failed: %s, then %s (%s)", params.get("job_id"),
                           ", ".join(f"{a['provider']} {a['outcome']} ({a['reason']})" for a in attempts),
                           backend, (str(error) or repr(error))[:200])
                raise
            reason = _failure_reason(error)
            attempts.append({"provider": backend, "outcome": "failed", "reason": reason,
                             "detail": (str(error) or repr(error))[:300]})
            _mark(backend, False, reason)
            continue
        if local:
            _mark(backend, True, "ok")
        attempts.append({"provider": backend, "outcome": "served"})
        entry["backend_attempts"] = attempts
        entry["served_provider"] = backend
        entry["billing"] = "local" if local else "hosted"
        if len(attempts) > 1:
            log.warning("backend-order fallback job=%s asked=%s served=%s after=%s",
                        params.get("job_id"), provider, backend,
                        ",".join(f"{a['provider']}:{a['reason']}" for a in attempts[:-1]))
        return media, backend
    raise RuntimeError("no backend could serve this job: " + ", ".join(
        f"{a['provider']} {a['outcome']} ({a['reason']})" for a in attempts))
