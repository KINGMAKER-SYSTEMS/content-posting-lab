"""Compare exported mature outcomes: python scripts/compare_beat_slideshows.py rows.json."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.beat_slideshow import summarize_results
print(json.dumps(summarize_results(json.loads(Path(sys.argv[1]).read_text())), indent=2))
