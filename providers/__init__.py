from . import grok, replicate

PROVIDERS = {
    "grok": {
        "name": "Grok",
        "group": "xAI",
        "key_id": "xai",
        "pricing": "~$5/10s video",
        "models": ["grok-imagine-video"],
        "module": grok,
    },
    "hailuo": {
        "name": "Hailuo 2.3",
        "group": "MiniMax",
        "key_id": "replicate",
        "pricing": "~$0.28/video",
        "models": ["minimax/hailuo-2.3"],
        "module": replicate,
    },
    "wan-t2v": {
        "name": "Text-to-Video (2.2 14B)",
        "group": "Wan",
        "key_id": "replicate",
        "pricing": "~$0.06/sec",
        "models": ["wan-video/wan-2.2-t2v-fast"],
        "module": replicate,
    },
    "wan-i2v": {
        "name": "Image-to-Video (2.2 14B)",
        "group": "Wan",
        "key_id": "replicate",
        "pricing": "~$0.06/sec",
        "models": ["wan-video/wan-2.2-i2v-a14b"],
        "module": replicate,
    },
    "wan-i2v-fast": {
        "name": "Image-to-Video Fast (2.2 14B)",
        "group": "Wan",
        "key_id": "replicate",
        "pricing": "~$0.06/sec",
        "models": ["wan-video/wan-2.2-i2v-fast"],
        "module": replicate,
    },
}
