"""Write contracts/caption-style.v1.json from the live CaptionStyle model."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.caption_render import CAPTION_STYLE_CONTRACT_PATH, caption_style_contract  # noqa: E402


def render() -> str:
    return json.dumps(caption_style_contract(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    CAPTION_STYLE_CONTRACT_PATH.parent.mkdir(exist_ok=True)
    CAPTION_STYLE_CONTRACT_PATH.write_text(render(), encoding="utf-8")
    print(CAPTION_STYLE_CONTRACT_PATH)
