#!/bin/zsh
# One truck scene from blockout to depth pass:  tools/blockout/make_shot.sh <scene>
# Scenes live on the drive (team storage rule): renders and .blend files are not kept on the internal disk.
set -e
scene=$1
B=/Volumes/OWC-Envoy-Ultra/content-posting-lab/blockout-trucks
EC=~/dev/ec/work/1121-blockout2video          # ec's blockout-to-video tools, used as they are
mkdir -p $B
cd ~/dev/content-posting-lab
size=(--width 1344 --height 768)
nframes=97
case $scene in
  # Christmas POV format: an eight-second portrait walk
  xmas-*) size=(--width 768 --height 1344); nframes=193 ;;
  # silhouette-truck format: a vertical still, so a handful of frames is enough
  silhouette-*) size=(--width 768 --height 1344); nframes=9 ;;
esac
blender -b -P tools/blockout/truck_scenes.py -- --scene $scene --out $B/$scene.blend --frames $nframes 2>&1 | /usr/bin/grep -a -E "TRUCK_SCENE_SAVED|Error|Traceback" || true
# --near pins the depth range to where the truck is; left to itself the range starts at the
# ground under the camera and the truck comes out nearly black, too faint to guide the model.
range=(--near 5 --far 300)
case $scene in
  # boat format: the skyline is 300 m off, which inverse depth leaves black, and the shot is handheld
  harbor-*) range=(--depth-mode linear --near 2 --far 460 --shake 0.8) ;;
  # a walk down a lane: the tree at the far end must still show, and the camera has footstep sway
  # from a car: a steadier camera and a longer view down the street
  xmas-drive-*) range=(--depth-mode linear --near 0.6 --far 120 --shake 0.35) ;;
  # standing still above a village: only a slight handheld settle, and the far ridge must show
  xmas-alpine-*) range=(--depth-mode linear --near 2 --far 520 --shake 0.4) ;;
  xmas-*) range=(--depth-mode linear --near 0.6 --far 95 --shake 1.0) ;;
esac
blender -b $B/$scene.blend -P $EC/blender/export_passes.py -- --out $B/$scene --preset ltx $size --frames $nframes --clay $range 2>&1 | /usr/bin/grep -a -E "B2V_|Error|Traceback" | cut -c1-200
