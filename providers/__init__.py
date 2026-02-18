from . import fal, grok, luma, replicate, sora

PROVIDERS = {
    "grok": {
        "name": "Grok Imagine",
        "key_id": "xai",
        "pricing": "~$5/10s video",
        "models": ["grok-imagine-video"],
        "module": grok,
    },
    "rep-minimax": {
        "name": "MiniMax Hailuo 2.3",
        "key_id": "replicate",
        "pricing": "~$0.28/video",
        "models": ["minimax/hailuo-2.3"],
        "module": replicate,
    },
    "rep-wan": {
        "name": "Wan 2.1 720p",
        "key_id": "replicate",
        "pricing": "~$0.06/sec",
        "models": ["wavespeedai/wan-2.1-t2v-720p"],
        "module": replicate,
    },
    "rep-kling": {
        "name": "Kling v2.1",
        "key_id": "replicate",
        "pricing": "~$0.07/sec",
        "models": ["kwaivgi/kling-v2.1-master"],
        "module": replicate,
    },
    "fal-wan": {
        "name": "Wan 2.5 (FAL)",
        "key_id": "fal",
        "pricing": "$0.05/sec",
        "models": ["fal-ai/wan-25-preview/text-to-video"],
        "module": fal,
    },
    "fal-kling": {
        "name": "Kling 2.5 (FAL)",
        "key_id": "fal",
        "pricing": "$0.07/sec",
        "models": ["fal-ai/kling-video/v2.5-turbo/pro"],
        "module": fal,
    },
    "fal-ovi": {
        "name": "Ovi (FAL)",
        "key_id": "fal",
        "pricing": "$0.20/video",
        "models": ["fal-ai/ovi"],
        "module": fal,
    },
    "luma": {
        "name": "Luma Ray 2",
        "key_id": "luma",
        "pricing": "~$1-2/video",
        "models": ["ray-2"],
        "module": luma,
    },
    "sora": {
        "name": "Sora 2",
        "key_id": "openai",
        "pricing": "~$0.10/sec (720p)",
        "models": ["sora-2"],
        "module": sora,
    },
}
