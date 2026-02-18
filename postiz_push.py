"""
Postiz TikTok Upload + Post Creation Script
Handles rate limits automatically (30 req/hr).
Run with: python3 postiz_push.py
"""
import subprocess, json, time, os, sys

API_KEY = "f1f93b9098cab88876c2724df1c6dc49f16c446296ec68186fbfcea4ccbffd73"
BASE = "https://api.postiz.com/public/v1"
FUNNEL = "https://risings-mac-mini-1.tail168656.ts.net"
STATE_FILE = "/tmp/postiz_push_state.json"

ACCOUNTS = {
    "cmlkh2y3z02qvol0ygga0shta": "Hayden",
    "cmlkhd78b02uool0ynkqd85sp": "Stony",
    "cmlkg6su502oxol0ysm6gi0d8": "Bart",
    "cmlkgxz6e02qjol0y8v42qtcv": "Cash",
    "cmlkgwkxd02q8ol0yt71xe2m5": "wayne",
}

TIKTOK_SETTINGS = {
    "__type": "tiktok",
    "privacy_level": "SELF_ONLY",
    "duet": False,
    "stitch": False,
    "comment": True,
    "autoAddMusic": "no",
    "brand_content_toggle": False,
    "brand_organic_toggle": False,
    "content_posting_method": "UPLOAD",
}

def build_video_list():
    videos = []
    # backroad 0-19 -> Hayden
    for i in range(20):
        videos.append(("cmlkh2y3z02qvol0ygga0shta", f"{FUNNEL}/backroad/burned_{i:03d}.mp4", f"backroad_{i:03d}"))
    # field 0-19 -> Stony
    for i in range(20):
        videos.append(("cmlkhd78b02uool0ynkqd85sp", f"{FUNNEL}/field/burned_{i:03d}.mp4", f"field_{i:03d}"))
    # field 20-30 + mlon8ttj 0-8 -> Bart
    for i in range(20, 31):
        videos.append(("cmlkg6su502oxol0ysm6gi0d8", f"{FUNNEL}/field/burned_{i:03d}.mp4", f"field_{i:03d}"))
    for i in range(9):
        videos.append(("cmlkg6su502oxol0ysm6gi0d8", f"{FUNNEL}/mlon8ttj/burned_{i:03d}.mp4", f"mlon8ttj_{i:03d}"))
    # mlon8ttj 9-19 + mlonirsa 0-8 -> Cash
    for i in range(9, 20):
        videos.append(("cmlkgxz6e02qjol0y8v42qtcv", f"{FUNNEL}/mlon8ttj/burned_{i:03d}.mp4", f"mlon8ttj_{i:03d}"))
    for i in range(9):
        videos.append(("cmlkgxz6e02qjol0y8v42qtcv", f"{FUNNEL}/mlonirsa/burned_{i:03d}.mp4", f"mlonirsa_{i:03d}"))
    # mlonirsa 9 + night parking lot 0-18 -> wayne
    videos.append(("cmlkgwkxd02q8ol0yt71xe2m5", f"{FUNNEL}/mlonirsa/burned_009.mp4", "mlonirsa_009"))
    for i in range(19):
        videos.append(("cmlkgwkxd02q8ol0yt71xe2m5", f"{FUNNEL}/night%20parking%20lot/burned_{i:03d}.mp4", f"nightlot_{i:03d}"))
    return videos

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"uploaded": {}, "posted": [], "request_count": 0, "window_start": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def check_rate_limit(state):
    now = time.time()
    if now - state["window_start"] > 3600:
        state["request_count"] = 0
        state["window_start"] = now
    if state["request_count"] >= 28:  # leave 2 buffer
        wait = 3600 - (now - state["window_start"]) + 10
        if wait > 0:
            print(f"\n⏳ Rate limit ({state['request_count']}/30). Waiting {int(wait/60)}m {int(wait%60)}s...")
            time.sleep(wait)
            state["request_count"] = 0
            state["window_start"] = time.time()

def api_call(method, endpoint, data=None, state=None):
    check_rate_limit(state)
    cmd = ["curl", "-s", "-X", method, f"{BASE}/{endpoint}",
           "-H", f"Authorization: {API_KEY}", "-H", "Content-Type: application/json"]
    if data:
        cmd += ["-d", json.dumps(data)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    state["request_count"] += 1
    save_state(state)
    try:
        return json.loads(result.stdout)
    except:
        return {"error": result.stdout[:500]}

def upload_video(url, state):
    resp = api_call("POST", "upload-from-url", {"url": url}, state)
    if "path" in resp:
        return resp["path"]
    if "429" in str(resp) or "Too Many" in str(resp.get("message", "")):
        print(f"  429 - forcing rate limit wait")
        state["request_count"] = 30
        check_rate_limit(state)
        # retry
        resp = api_call("POST", "upload-from-url", {"url": url}, state)
        if "path" in resp:
            return resp["path"]
    print(f"  Upload error: {resp}")
    return None

def create_posts(integration_id, videos_with_paths, state):
    posts_array = []
    for tag, path in videos_with_paths:
        posts_array.append({
            "integration": {"id": integration_id},
            "value": [{"content": "", "image": [{"id": tag, "path": path}]}],
            "settings": TIKTOK_SETTINGS,
        })
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    resp = api_call("POST", "posts", {
        "type": "now", "date": now, "shortLink": False, "tags": [], "posts": posts_array
    }, state)
    if isinstance(resp, list):
        return len(resp)
    print(f"  Post creation error: {resp}")
    return 0

def main():
    videos = build_video_list()
    state = load_state()
    
    print(f"{'='*60}")
    print(f"POSTIZ TIKTOK PUSH - {len(videos)} videos to 5 accounts")
    print(f"Already uploaded: {len(state['uploaded'])}")
    print(f"Already posted: {len(state['posted'])}")
    print(f"Requests this window: {state['request_count']}")
    print(f"{'='*60}\n")

    # Phase 1: Upload all videos
    for integration_id, url, tag in videos:
        key = f"{integration_id}|{tag}"
        if key in state["uploaded"]:
            continue
        
        print(f"Uploading {tag} for {ACCOUNTS[integration_id]}...", end=" ", flush=True)
        path = upload_video(url, state)
        if path:
            state["uploaded"][key] = path
            print(f"✅ ({len(state['uploaded'])}/100)")
        else:
            print("❌ skipping")
    
    print(f"\n{'='*60}")
    print(f"Uploads complete: {len(state['uploaded'])}/100")
    print(f"{'='*60}\n")

    # Phase 2: Create posts grouped by account
    for integration_id, name in ACCOUNTS.items():
        # Get uploaded videos for this account that haven't been posted
        vids = []
        for key, path in state["uploaded"].items():
            iid, tag = key.split("|", 1)
            if iid == integration_id and key not in state["posted"]:
                vids.append((tag, path))
        
        if not vids:
            print(f"{name}: nothing to post")
            continue
        
        print(f"Creating {len(vids)} posts for {name}...", end=" ", flush=True)
        count = create_posts(integration_id, vids, state)
        if count > 0:
            for tag, path in vids:
                state["posted"].append(f"{integration_id}|{tag}")
            save_state(state)
            print(f"✅ {count} posts created")
        else:
            print("❌ failed")

    print(f"\n{'='*60}")
    print(f"DONE!")
    print(f"Uploaded: {len(state['uploaded'])}/100")
    print(f"Posted: {len(state['posted'])}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
