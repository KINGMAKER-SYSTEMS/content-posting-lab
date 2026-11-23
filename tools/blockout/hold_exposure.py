"""Hold a clip's exposure at its opening level.

    python tools/blockout/hold_exposure.py IN.mp4 OUT.mp4

LTX let the Christmas lane walk drift brighter over its eight seconds (the Lab's fade-in check
compares the opening with the rest and failed it at under 0.7). This scales each frame so its
mean brightness matches the average of the first six frames: one gain per frame, smoothed over
time and limited to 0.55-1.25, so it corrects drift and leaves the picture alone otherwise.
"""
from __future__ import annotations

import subprocess
import sys

import numpy as np


def probe(path: str) -> tuple[int, int, str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout.strip().split(",")
    return int(out[0]), int(out[1]), out[2]


def main() -> None:
    src, dest = sys.argv[1], sys.argv[2]
    width, height, rate = probe(src)
    size = width * height * 3
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, height, width, 3)
    luma = frames.reshape(len(frames), -1, 3).astype(np.float32) @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    means = luma.mean(axis=1)
    target = means[:6].mean()
    gains = np.clip(target / np.maximum(means, 1e-3), 0.55, 1.25)
    kernel = np.ones(9) / 9                                   # smooth, so the gain never flickers
    gains = np.convolve(np.pad(gains, 4, mode="edge"), kernel, mode="valid")
    out = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s",
                            f"{width}x{height}", "-r", rate, "-i", "-", "-an", "-c:v", "libx264", "-crf", "16",
                            "-pix_fmt", "yuv420p", dest], stdin=subprocess.PIPE)
    for frame, gain in zip(frames, gains):
        out.stdin.write(np.clip(frame.astype(np.float32) * gain, 0, 255).astype(np.uint8).tobytes())
    out.stdin.close()
    out.wait()
    print(f"mean brightness {means[0]:.1f} -> {means[-1]:.1f} before; gains {gains.min():.2f}-{gains.max():.2f}")


if __name__ == "__main__":
    main()
