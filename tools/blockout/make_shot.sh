#!/bin/zsh
# One truck scene from blockout to depth pass:  tools/blockout/make_shot.sh <scene>
# Scenes live on the drive (team storage rule): renders and .blend files are not kept on the internal disk.
set -e
scene=$1
B=/Volumes/OWC-Envoy-Ultra/content-posting-lab/blockout-trucks
EC=~/dev/ec/work/1121-blockout2video          # ec's blockout-to-video tools, used as they are
mkdir -p $B
cd ~/dev/content-posting-lab
blender -b -P tools/blockout/truck_scenes.py -- --scene $scene --out $B/$scene.blend 2>&1 | /usr/bin/grep -a -E "TRUCK_SCENE_SAVED|Error|Traceback" || true
# --near pins the depth range to where the truck is; left to itself the range starts at the
# ground under the camera and the truck comes out nearly black, too faint to guide the model.
blender -b $B/$scene.blend -P $EC/blender/export_passes.py -- --out $B/$scene --preset ltx --width 1344 --height 768 --clay --near 5 --far 300 2>&1 | /usr/bin/grep -a -E "B2V_|Error|Traceback" | cut -c1-200
