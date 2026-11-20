import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("A1MA_CONFIG_DIR", Path.home() / ".config" / "a1ma"))
DATA_DIR = Path(os.environ.get("A1MA_DATA_DIR", Path.home() / ".local" / "share" / "a1ma"))
CONFIG_PATH = CONFIG_DIR / "config.json"

PROVIDERS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": ["OPENROUTER_API_KEY", "A1MA_API_KEY"],
        "default_model": "cohere/north-mini-code:free",
        # tried in order when the primary model rate-limits
        "default_fallbacks": [
            "nvidia/nemotron-3.5-lightning:free",
            "poolside/laguna-xs-2.1:free",
            "google/gemma-4-26b-a4b-it:free",
            "dots-studio/dots-3-note-preview:free",
        ],
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "key_env": ["NVIDIA_API_KEY", "A1MA_NVIDIA_API_KEY"],
        "default_model": "nvidia/nemotron-3-ultra-550b-a55b",
        # tool-capable models verified against this endpoint (2026-10)
        "default_fallbacks": [
            "nvidia/nemotron-3.5-lightning-30b-a3b",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
            "meta/muse-glimmer-30b",
        ],
    },
}


def _read_raw() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def load() -> dict:
    raw = _read_raw()
    provider = os.environ.get("A1MA_PROVIDER") or raw.get("provider") or "openrouter"
    if provider not in PROVIDERS:
        provider = "openrouter"
    p = PROVIDERS[provider]
    # flat fields in the file belong to whichever provider was active last save
    flat_owner = raw.get("provider") or "openrouter"
    saved = dict((raw.get("providers") or {}).get(provider) or {})
    if provider == flat_owner:
        # flat fields are authoritative for the provider that wrote them;
        # the per-provider block may be a stale stash from an earlier switch
        flat = {
            "model": raw.get("model"),
            "base_url": raw.get("base_url"),
            "fallback_models": raw.get("fallback_models"),
            "council": raw.get("council"),
            "api_key": (raw.get("api_key") if flat_owner == "openrouter"
                        else raw.get("nvidia_api_key")),
        }
        saved.update({k: v for k, v in flat.items() if v})

    keys = {
        "openrouter": raw.get("api_key") or "",
        "nvidia": raw.get("nvidia_api_key") or "",
    }
    key = (os.environ.get(p["key_env"][0])
           or (os.environ.get(p["key_env"][1]) if len(p["key_env"]) > 1 else None)
           or saved.get("api_key")
           or keys.get(provider)
           or "")

    council = dict(saved.get("council") or {})
    # enable/disable is a global preference (not per provider)
    council["enabled"] = bool(raw.get("council_enabled", False))

    return {
        "provider": provider,
        "api_key": key,
        "keys": keys,
        "providers": raw.get("providers") or {},
        "model": (os.environ.get("A1MA_MODEL") or saved.get("model")
                  or p["default_model"]),
        "base_url": saved.get("base_url") or p["base_url"],
        "fallback_models": (saved.get("fallback_models")
                            or p["default_fallbacks"]),
        "max_tokens": raw.get("max_tokens") or 4096,
        "council": council,
    }


def switch(cfg: dict, new_provider: str) -> dict:
    """Move active settings into providers[old], load providers[new] as active."""
    if new_provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {new_provider}")
    old = cfg.get("provider", "openrouter")
    if new_provider == old:
        return cfg

    providers = dict(cfg.get("providers") or {})
    enabled = (cfg.get("council") or {}).get("enabled", False)
    providers[old] = {
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "fallback_models": cfg["fallback_models"],
        # model slots only — on/off is global
        "council": {k: v for k, v in (cfg.get("council") or {}).items()
                    if k != "enabled"},
        "api_key": cfg.get("keys", {}).get(old) or cfg.get("api_key") or "",
    }
    saved = providers.get(new_provider) or {}
    p = PROVIDERS[new_provider]

    keys = dict(cfg.get("keys") or {})
    new_council = dict(saved.get("council") or {})
    new_council["enabled"] = enabled
    cfg.update({
        "provider": new_provider,
        "providers": providers,
        "keys": keys,
        "api_key": keys.get(new_provider) or saved.get("api_key") or "",
        "model": saved.get("model") or p["default_model"],
        "base_url": p["base_url"],
        "fallback_models": saved.get("fallback_models") or p["default_fallbacks"],
        "council": new_council,
    })
    return cfg


def council_cfg(cfg: dict) -> dict:
    """Resolve council settings: enabled flag + two distinct model slots."""
    c = cfg.get("council") or {}
    model_a = c.get("model_a") or cfg["model"]
    model_b = c.get("model_b") or next(
        (m for m in cfg.get("fallback_models", []) if m != model_a), cfg["model"]
    )
    if model_b == model_a:  # degenerate: fall back to first different fallback
        model_b = next(
            (m for m in cfg.get("fallback_models", []) if m != model_a), model_b
        )
    return {"enabled": bool(c.get("enabled", False)),
            "model_a": model_a, "model_b": model_b}


def save(cfg: dict) -> None:
    raw = _read_raw()
    keys = cfg.get("keys") or {}
    raw.update({
        "provider": cfg.get("provider", "openrouter"),
        "council_enabled": bool((cfg.get("council") or {}).get("enabled", False)),
        "api_key": keys.get("openrouter") or raw.get("api_key") or "",        "nvidia_api_key": keys.get("nvidia") or raw.get("nvidia_api_key") or "",
        "max_tokens": cfg.get("max_tokens") or raw.get("max_tokens") or 4096,
        # active provider's settings stay flat (back-compat + what load reads first)
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "fallback_models": cfg["fallback_models"],
        "council": cfg.get("council") or {},
        # drop the active provider's stash: its flat fields are authoritative
        "providers": {k: v for k, v in (cfg.get("providers") or {}).items()
                      if k != cfg.get("provider", "openrouter")},
    })
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(raw, indent=2) + "\n")
    os.chmod(CONFIG_PATH, 0o600)
