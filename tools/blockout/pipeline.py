"""Truck clips by the Blender blockout route, one scene end to end.

    python tools/blockout/pipeline.py lakeside [mountain-pullout ...] [--tag v2] [--no-clean]

For each scene:
  1. build the blockout in Blender and export its depth pass (make_shot.sh)
  2. four look stills held to that depth (ec's lookdev.py), keep the one the judge faults least
  3. clean emblems, badges, lettering and plates off it (MiniMax H3-Image; tools/h3_image.py)
  4. depth-guided video on LTX with the cleaned still as frame one (ec's run_shot.py)
  5. score the clip as the Lab would post it: five tall slices, each judged on its own

Scenes, stills, videos and results.jsonl live on the drive under
/Volumes/OWC-Envoy-Ultra/content-posting-lab/blockout-trucks (team storage rule).
The depth, look-still and video scripts belong to ec's 1121-blockout2video lane and are
run as they are; nothing in that folder is changed.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

import h3_image  # noqa: E402
import recipe_lab as rl  # noqa: E402

ROOT = Path("/Volumes/OWC-Envoy-Ultra/content-posting-lab/blockout-trucks")
EC = Path.home() / "dev/ec/work/1121-blockout2video/comfy"

TRUCK = ("a real dark navy-black full-size crew-cab pickup truck with glossy factory paint, a plain dark grille, "
         "plain dark alloy wheels and clear empty windows, completely unmarked bodywork")
LOOK = "moody low-exposure phone photograph, muted natural colour, deep blacks, photorealistic"
STILL = "A dark navy-black full-size crew-cab pickup truck parked completely still, its wheels not turning"
FOOTAGE = "moody low-exposure phone footage with deep blacks and muted natural colour"

# still = the look-still prompt (what the scene is); video = what moves and how the camera travels.
SCENES = {
    "lakeside": {
        "still": f"Extreme wide low wheel-height photograph of {TRUCK}, parked on a gravel shoulder beside a still pond, "
                 f"dormant tan grass on a low hillside across the water, dark soft-overcast light, {LOOK}",
        "video": f"{STILL} on a gravel shoulder beside a still pond, dormant tan grass stirring slightly in a light "
                 f"breeze, faint ripples on the water, dark soft-overcast light, {FOOTAGE}, slow steady camera push-in",
        "strength": 0.7},
    "mountain-pullout": {
        "still": f"Extreme wide low wheel-height rear three-quarter photograph of {TRUCK}, seen from behind, parked on a "
                 f"gravel mountain pullout, a dense forest of tall natural pine trees close behind and grey hazy peaks "
                 f"beyond, dark soft-overcast light, {LOOK}",
        "video": f"{STILL} on a gravel mountain pullout, tall dark pines behind swaying very slightly, thin grey haze "
                 f"drifting across the peaks, dark soft-overcast light, {FOOTAGE}, slow steady camera drift",
        # With no trees blocked the still has little else to hold: at 0.8 the truck was drawn about
        # twice its blocked size. At 1.0 it kept its size, but the nearest ground (flat white in the
        # depth pass) was painted as a hard dark band across the bottom quarter of the frame.
        "strength": 0.9},
    "open-field": {
        "still": f"Extreme wide low wheel-height side profile photograph of {TRUCK}, parked on a dirt farm track across an "
                 f"open field of dry grass, a weathered wooden fence line behind and a distant dark tree line, heavy dark "
                 f"overcast sky at dusk, {LOOK}",
        "video": f"{STILL} on a dirt farm track in an open field, dry grass in the foreground moving gently in the wind, "
                 f"a wooden fence line behind, heavy dark overcast sky at dusk, {FOOTAGE}, slow steady push-in straight toward the side of the parked truck",
        "strength": 0.8},
    "desert-road": {
        "still": f"Extreme wide low wheel-height front three-quarter photograph of {TRUCK}, parked on an empty two-lane "
                 f"desert road, low scrub on both sides and flat-topped mesas on the far horizon, blue-hour dusk light, {LOOK}",
        "video": f"{STILL} on an empty two-lane desert road, low scrub trembling in a light breeze, flat-topped mesas on "
                 f"the far horizon, blue-hour dusk light, {FOOTAGE}, slow steady camera push-in",
        "strength": 0.8},
    "hilltop": {
        "still": f"Extreme wide low wheel-height rear three-quarter photograph of {TRUCK}, seen from behind, parked on a "
                 f"grassy rise with rolling hills falling away into haze, dark soft-overcast light before rain, {LOOK}",
        "video": f"{STILL} on a grassy rise, long grass moving in the wind, rolling hills fading into haze, dark "
                 f"soft-overcast light before rain, {FOOTAGE}, slow steady camera drift",
        "strength": 0.8},
    "salt-flat": {
        "still": f"Extreme wide low wheel-height side profile photograph of {TRUCK}, parked on a vast empty flat plain of "
                 f"pale cracked earth, a thin dark mountain range on the far horizon, deep dusk light, {LOOK}",
        "video": f"{STILL} on a vast empty flat plain, a thin veil of dust drifting low across the ground, a dark mountain "
                 f"range on the far horizon, deep dusk light, {FOOTAGE}, slow steady camera push-in",
        "strength": 0.8},
    "snow-field": {
        "still": f"Extreme wide low wheel-height front three-quarter photograph of {TRUCK}, parked in a snow-covered "
                 f"field in deep winter, smooth untouched snow all around, low drifts and pale hills fading into grey "
                 f"haze, flat dark overcast light at dusk, {LOOK}",
        "video": f"{STILL} in a snow-covered field in deep winter, light snow falling slowly, fine powder blowing low "
                 f"across the drifts, pale hills in grey haze, flat dark overcast light at dusk, {FOOTAGE}, slow steady "
                 f"camera push-in",
        "strength": 0.8},
    "beach": {
        "still": f"Extreme wide low wheel-height photograph of {TRUCK}, parked on a wide empty beach of flat wet sand at "
                 f"low tide, grey sea to the horizon and a dark headland far off, heavy overcast sky at dusk, {LOOK}",
        "video": f"{STILL} on a wide empty beach of flat wet sand at low tide, small waves rolling in far behind, thin "
                 f"sea mist drifting, heavy overcast sky at dusk, {FOOTAGE}, slow sideways camera drift",
        "strength": 0.8},
    "barn": {
        "still": f"Extreme wide low wheel-height side profile photograph of {TRUCK}, parked in a gravel farmyard, a large "
                 f"weathered plain wooden barn and a metal grain silo behind, a wooden fence along the yard, dark "
                 f"soft-overcast light at dusk, {LOOK}",
        "video": f"{STILL} in a gravel farmyard, a weathered wooden barn and a grain silo behind, dry weeds along the "
                 f"fence moving in a light wind, dark soft-overcast light at dusk, {FOOTAGE}, slow steady camera push-in",
        "strength": 0.8},
    # Second set (2026-11-22). 0.9 throughout: at 0.8 the barn stills drew the truck anywhere from
    # its blocked size to twice that.
    "forest-road": {
        "still": f"Extreme wide low wheel-height front three-quarter photograph of {TRUCK}, parked on a damp dirt forest "
                 f"road, a dense forest of very tall natural fir and pine trees on both sides, thin mist between the "
                 f"trunks, dark soft-overcast light, {LOOK}",
        "video": f"{STILL} on a damp dirt forest road, tall firs and pines on both sides swaying very slightly, thin mist "
                 f"drifting between the trunks, dark soft-overcast light, {FOOTAGE}, slow steady camera push-in",
        "strength": 0.9},
    "canyon-rim": {
        "still": f"Extreme wide low wheel-height rear three-quarter photograph of {TRUCK}, seen from behind, parked on bare "
                 f"rock at the rim of a vast desert canyon, layered red-brown canyon walls and buttes falling away beyond "
                 f"it into blue haze, dusk light after sunset, {LOOK}",
        "video": f"{STILL} on bare rock at the rim of a vast desert canyon, haze drifting slowly through the canyon far "
                 f"below, dusk light after sunset, {FOOTAGE}, slow steady camera drift",
        "strength": 0.9},
    "hay-field": {
        "still": f"Extreme wide low wheel-height side profile photograph of {TRUCK}, parked in a mown hay field of short "
                 f"golden stubble, large round hay bales scattered across the field behind, low rises on the horizon, "
                 f"heavy dark overcast sky at dusk, {LOOK}",
        "video": f"{STILL} in a mown hay field, round hay bales scattered behind, stubble and loose straw stirring in "
                 f"a light wind, heavy dark overcast sky at dusk, {FOOTAGE}, slow steady push-in straight toward the side of the parked truck",
        "strength": 0.9},
    "cabin-snow": {
        "still": f"Extreme wide low wheel-height front three-quarter photograph of {TRUCK}, parked in a snowy forest "
                 f"clearing in deep winter, a small log cabin with a snow-covered roof and one warm lit window behind it, "
                 f"tall snow-laden pine trees around the clearing, blue dusk light, {LOOK}",
        "video": f"{STILL} in a snowy forest clearing, a small log cabin with one warm lit window behind, snow falling "
                 f"slowly, thin smoke rising from the cabin chimney, snow-laden pines, blue dusk light, {FOOTAGE}, slow "
                 f"steady camera push-in",
        "strength": 0.9},
    "lake-dock": {
        "still": f"Extreme wide low wheel-height side profile photograph of {TRUCK}, parked on a gravel launch at the edge "
                 f"of a calm lake, a long weathered wooden dock running out over the water behind it, dark forested hills "
                 f"on the far shore, mist on the water, dark soft-overcast light at dawn, {LOOK}",
        "video": f"{STILL} on a gravel launch at the edge of a calm lake, a weathered wooden dock running out over the "
                 f"water, faint ripples and mist drifting on the lake, dark soft-overcast light at dawn, {FOOTAGE}, slow "
                 f"steady camera push-in",
        "strength": 0.9},
    "night-lot": {
        "still": f"Extreme wide low wheel-height front three-quarter photograph of {TRUCK}, parked alone in a large empty "
                 f"asphalt lot at night, wet ground after rain, one tall street lamp casting a pool of warm light over "
                 f"the truck, deep black sky, {LOOK}",
        "video": f"{STILL} alone in a large empty asphalt lot at night, wet ground reflecting one tall street lamp, light "
                 f"rain falling through the lamp light, deep black sky, {FOOTAGE}, slow steady camera push-in",
        "strength": 0.9},
    # Winter / Christmas set (2026-11-22).
    "tree-farm": {
        "still": f"Extreme wide low wheel-height side profile photograph of {TRUCK}, a freshly cut Christmas tree wrapped "
                 f"in netting lying in its open bed, parked on a snowy track at a Christmas tree farm, long rows of small "
                 f"snow-dusted evergreen trees behind, flat dark overcast light at dusk in deep winter, {LOOK}",
        "video": f"{STILL} at a Christmas tree farm in deep winter, a cut Christmas tree lying in its bed, rows of small "
                 f"snow-dusted evergreens behind, light snow falling slowly, flat dark overcast light at dusk, {FOOTAGE}, "
                 f"slow steady push-in straight toward the side of the parked truck",
        "strength": 0.9},
    "covered-bridge": {
        "still": f"Extreme wide low wheel-height front three-quarter photograph of {TRUCK}, parked on a snowy lane in front "
                 f"of an old weathered wooden covered bridge over a frozen creek, bare winter trees and snow-covered banks, "
                 f"flat dark overcast light at dusk, {LOOK}",
        "video": f"{STILL} on a snowy lane in front of an old wooden covered bridge, light snow falling slowly, bare "
                 f"winter trees, flat dark overcast light at dusk, {FOOTAGE}, slow steady camera push-in",
        "strength": 0.9},
    "frozen-lake": {
        "still": f"Extreme wide low wheel-height rear three-quarter photograph of {TRUCK}, seen from behind, parked out on "
                 f"a vast frozen lake, smooth snow-dusted ice to the horizon, a thin dark line of pine forest on the far "
                 f"shore, pale blue dusk light in deep winter, {LOOK}",
        "video": f"{STILL} out on a vast frozen lake, fine snow blowing low across the ice, a dark line of pine forest on "
                 f"the far shore, pale blue dusk light, {FOOTAGE}, slow steady camera drift",
        "strength": 0.9},
    "cabin-lights": {
        "still": f"Extreme wide low wheel-height front three-quarter photograph of {TRUCK}, parked in a snowy forest "
                 f"clearing on Christmas Eve, a small log cabin behind it with warm string lights along its snowy roof and "
                 f"glowing windows, a decorated Christmas tree with warm white lights standing outside in the snow, tall "
                 f"snow-laden pines, blue dusk light, {LOOK}",
        "video": f"{STILL} in a snowy forest clearing on Christmas Eve, a log cabin with warm string lights and glowing "
                 f"windows, a Christmas tree with warm white lights twinkling gently outside, snow falling slowly, blue "
                 f"dusk light, {FOOTAGE}, slow steady camera push-in",
        "strength": 0.9},
    "barn-snow": {
        "still": f"Extreme wide low wheel-height side profile photograph of {TRUCK}, parked in a snow-covered farmyard in "
                 f"deep winter, a large weathered wooden barn with snow on its roof and a big evergreen wreath on its "
                 f"door, a metal grain silo behind, a wooden fence along the yard, blue dusk light, {LOOK}",
        "video": f"{STILL} in a snow-covered farmyard in deep winter, a weathered barn with an evergreen wreath on its "
                 f"door and a grain silo behind, snow falling slowly, blue dusk light, {FOOTAGE}, slow steady camera push-in",
        "strength": 0.9},
    "pass-snow": {
        "still": f"Extreme wide low wheel-height rear three-quarter photograph of {TRUCK}, seen from behind, parked on a "
                 f"snow-covered mountain pullout in deep winter, a dense forest of tall snow-laden pine trees close behind "
                 f"and white peaks beyond in grey haze, flat dark overcast light, {LOOK}",
        "video": f"{STILL} on a snow-covered mountain pullout, tall snow-laden pines behind, snow falling slowly, white "
                 f"peaks fading into grey haze, flat dark overcast light, {FOOTAGE}, slow steady camera drift",
        "strength": 0.9},
    # Boat format (the Lab's boat-lake). The video prompts are the Lab's own, word for word; the
    # still is the opening frame, which in this blockout is only water. Judged with the boat spec,
    # and not sent through the truck cleaning pass.
    "harbor-night": {
        "still": "POV phone photograph from onboard a speedboat at night, looking down over the side at dark choppy "
                 "harbor water rushing past, white wake foam and spray, bright fluorescent city lights reflected in "
                 "long streaks on the black water, raw handheld look, deep blacks, photorealistic",
        "video": "POV Tik Tok style video from onboard a speedboat at night in the harbor of Shanghai, the moody dark "
                 "waters reflect the towers skyline's bright fluorescent lights that adorn the scaffolding, cinematic "
                 "raw vibes, camera pans from a view of the dark choppy water to a handheld semi shaky reveal of the "
                 "city skyline lit up at night",
        "strength": 0.6, "spec": "boat", "clean": False},
    "harbor-dusk": {
        "still": "POV phone photograph from onboard a fast speedboat at dusk, looking down over the side at dark choppy "
                 "harbor water rushing past, spray and wake foam, neon and pastel sunset reflections streaking across "
                 "the waves, raw handheld look, photorealistic",
        "video": "POV TikTok-style video from onboard a fast speedboat at dusk in Singapore Marina Bay. Fast hook "
                 "opening: camera starts low and tight on dark choppy water rushing past the boat, neon and sunset "
                 "reflections streaking across the waves, water spray hitting the lens, intense speed and handheld "
                 "shake. After 1 second, the camera does a quick cinematic whip-tilt / pan up from the water into a "
                 "dramatic reveal of the Singapore skyline, glowing high-rise tower lights and pastel dusk sky "
                 "reflecting on the moody harbor water. Raw nightlife energy, semi-shaky handheld phone footage, fast "
                 "zoom feeling, cinematic but realistic",
        "strength": 0.6, "spec": "boat", "clean": False},
    # Christmas POV format (the Lab's pov-seasonal): eight-second portrait walks. The video prompts are
    # the Lab's own scene sentences inside its fixed wrapper; the destination is in the first frame,
    # so one pinned still is enough.
    "xmas-lane": {
        "still": "Portrait photograph taken at eye level looking down an empty narrow snow-dusted cobblestone lane at "
                 "deep blue hour, old stone houses on both sides with warm glowing bay windows and fir swags, a "
                 "stone doorway with a red bow and an amber lantern on the left, a tall Christmas tree covered in "
                 "small warm-white lights in the square at the end of the lane, natural light falloff, photorealistic",
        "video": "One unbroken eight-second portrait shot from an adult's eye level, already walking at a calm pace. "
                 "Take a few natural steps with slight footstep sway and a steady horizon. A nearby detail passes off "
                 "the edge while the destination already visible ahead gradually grows larger. Walk down a "
                 "snow-dusted cobblestone lane at deep blue hour. A red bow and an amber lantern on a stone doorway "
                 "pass close on the left. Warm bay windows alternate with fir swags along the lane. A tall Christmas "
                 "tree covered in small steady warm-white lights anchors the square ahead. Keep paths, buildings and "
                 "bulbs consistent. Preserve detail in the snow and individual bulbs. Natural light falloff, no "
                 "pulsing or blown-out whites. Maintain coherent geometry and steady practical Christmas lights through the final frame.",
        # hold_exposure: the raw eight-second render brightened from 64 to 110 (lane x1)
        "strength": 0.7, "hold_exposure": True, "spec": "christmas", "clean": False},
    "xmas-market": {
        # "an adult's eye level" put an adult in every still, so the still prompts say only "eye level",
        # and the market is described as empty after closing
        "still": "Portrait photograph taken at eye level looking down the empty, deserted aisle between cedar-roofed "
                 "wooden Christmas market stalls late at night after closing, a wreath with red berries on a post "
                 "at the right, a counter of wrapped boxes and green garland, warm stall light pooling on the bare "
                 "cobbles, the aisle opening toward a decorated Christmas tree with warm lights in an empty snowy "
                 "town square, photorealistic",
        "video": "One unbroken eight-second portrait shot from an adult's eye level, already walking at a calm pace. "
                 "Take a few natural steps with slight footstep sway and a steady horizon. A nearby detail passes off "
                 "the edge while the destination already visible ahead gradually grows larger. Walk between "
                 "cedar-roofed Christmas stalls at night. A foreground wreath with red berries passes on the right, "
                 "followed by a counter of wrapped boxes and green garland. The aisle opens toward a decorated tree "
                 "in a snowy town square. Warm stall light pools on the cobbles. Keep paths, buildings and bulbs "
                 "consistent. Natural light falloff, no pulsing or blown-out whites. Maintain coherent geometry and steady practical Christmas lights through the final frame.",
        # hold_exposure: the raw eight-second render brightened from 64 to 110 (lane x1)
        "strength": 0.7, "hold_exposure": True, "spec": "christmas", "clean": False},
    # Four more of the Lab's Christmas POV scenes (same wrapper sentences, the Lab's own scene text).
    "xmas-bridge": {
        "still": 'Portrait photograph taken at eye level from inside an empty short covered timber footbridge at blue twilight, amber bulbs tucked into fir garland along its wooden railings, a red ribbon tied to a near post on the left, warm light inside the bridge, and through the far opening a snowy row of timber shops and a tall spruce, photorealistic',
        "video": "One unbroken eight-second portrait shot from an adult's eye level, already walking at a calm pace. Take a few natural steps with slight footstep sway and a steady horizon. A nearby detail passes off the edge while the destination already visible ahead gradually grows larger. Walk across a short timber footbridge with amber bulbs tucked into garland on its railing. A red ribbon tied to a near post passes to the left. Through the far opening, a snowy row of timber shops and a tall spruce are already visible. Blue twilight outside and warm light inside the bridge. Keep paths, buildings and bulbs consistent. Natural light falloff, no pulsing or blown-out whites. Maintain coherent geometry and steady practical Christmas lights through the final frame.",
        "strength": 0.7, "hold_exposure": True, "spec": "christmas", "clean": False},
    "xmas-bakery": {
        "still": 'Portrait photograph taken at eye level looking along an empty stone pedestrian street at dusk toward a warmly lit bakery on the corner, a glowing shop window with a wreath and gingerbread houses close on the left, a barrel wrapped in fir on the right, warm lights along the gables, a small Christmas tree at the end of the lane, light snow on the stones, photorealistic',
        "video": "One unbroken eight-second portrait shot from an adult's eye level, already walking at a calm pace. Take a few natural steps with slight footstep sway and a steady horizon. A nearby detail passes off the edge while the destination already visible ahead gradually grows larger. Walk slowly along a stone pedestrian street toward a warmly lit bakery corner. A close shop window glows behind a wreath and gingerbread houses. A barrel wrapped in fir passes on the right. Warm lights on the gables lead toward a small tree at the end of the lane. Keep paths, buildings and bulbs consistent. Natural light falloff, no pulsing or blown-out whites. Maintain coherent geometry and steady practical Christmas lights through the final frame.",
        "strength": 0.7, "hold_exposure": True, "spec": "christmas", "clean": False},
    "xmas-chapel": {
        "still": 'Portrait photograph taken at eye level on an empty narrow snowy footpath leading to a small timber chapel, a wreath above its doorway and a lit Christmas tree to the side, a lit lantern set on a low stone wall close on the right, soft fine snowflakes drifting through the light, blue winter sky, warm doors and crisp snow, photorealistic',
        "video": "One unbroken eight-second portrait shot from an adult's eye level, already walking at a calm pace. Take a few natural steps with slight footstep sway and a steady horizon. A nearby detail passes off the edge while the destination already visible ahead gradually grows larger. Walk on a narrow footpath toward a small timber chapel, wreath above the doorway and a Christmas tree to the side. A lit lantern set on the stone wall passes near the lens. Soft fine flakes drift through the light. Blue winter sky, warm doors and crisp snow. Keep paths, buildings and bulbs consistent. Natural light falloff, no pulsing or blown-out whites. Maintain coherent geometry and steady practical Christmas lights through the final frame.",
        "strength": 0.7, "hold_exposure": True, "spec": "christmas", "clean": False},
    "xmas-mews": {
        "still": 'Portrait photograph taken at eye level looking down an empty narrow pedestrian mews lined with low redbrick homes at dusk, a pine swag with brass bells close on the left, a pair of warm candlelit windows on the right, hanging stars strung across the lane farther ahead and a lit potted spruce where the lane gently curves, a dusting of snow, photorealistic',
        "video": "One unbroken eight-second portrait shot from an adult's eye level, already walking at a calm pace. Take a few natural steps with slight footstep sway and a steady horizon. A nearby detail passes off the edge while the destination already visible ahead gradually grows larger. Walk down a narrow pedestrian mews lined with redbrick homes. Pass a close pine swag with brass bells on the left and a pair of warm candlelit windows on the right. Farther ahead, hanging stars and a lit potted spruce mark the gently curving lane. Keep paths, buildings and bulbs consistent. Natural light falloff, no pulsing or blown-out whites. Maintain coherent geometry and steady practical Christmas lights through the final frame.",
        "strength": 0.7, "hold_exposure": True, "spec": "christmas", "clean": False},
    # The Lab's five suburban-drive scenes and two of its Alpine overlooks. The video prompt is the
    # Lab's complete prompt, read from its catalog, so nothing of its wording is left out.
    "xmas-drive-candy": {
        "still": 'Portrait photograph taken from the front passenger seat of a car on an empty quiet residential street, the camera held up close to the clean windshield and aimed forward and slightly right, so the street and houses fill the whole picture and only the very top edge of the dark dashboard shows as a thin line along the bottom, after dark, a close snowy lawn with red-and-white candy canes and red porch bows on the right, and beyond it a two-story house with red, green and warm-white lights along its rooflines and a large glowing Christmas tree, snow along the kerbs, damp asphalt, photorealistic',
        "video": rl.catalog_prompt("pov-seasonal", "christmas-pov", 30)[0],
        "strength": 0.7, "hold_exposure": True, "trim_dash": True, "spec": "christmas", "clean": False},
    "xmas-drive-icicle": {
        "still": 'Portrait photograph taken from the front passenger seat of a car on an empty quiet residential street, the camera held up close to the clean windshield and aimed forward and slightly right, so the street and houses fill the whole picture and only the very top edge of the dark dashboard shows as a thin line along the bottom, at blue hour, large snowy houses on the right, the first with warm-white icicle lights, glowing wreaths in each window and a line of little lit trees along the kerb, the next with a broad porch wrapped in garland and a tall tree covered in tiny golden lights, snow along the kerbs, damp asphalt, photorealistic',
        "video": rl.catalog_prompt("pov-seasonal", "christmas-pov", 35)[0],
        "strength": 0.7, "hold_exposure": True, "trim_dash": True, "spec": "christmas", "clean": False},
    "xmas-drive-victorian": {
        "still": 'Portrait photograph taken from the front passenger seat of a car on an empty quiet residential street, the camera held up close to the clean windshield and aimed forward and slightly right, so the street and houses fill the whole picture and only the very top edge of the dark dashboard shows as a thin line along the bottom, at dusk, two tall painted Victorian homes on the right, the near one with amber lights tracing its porch and bay windows, deep green garland and red ribbons, the next with a glowing outdoor tree and red-and-gold bulbs around its roofline, small garden lights close by, snow along the kerbs, damp asphalt, photorealistic',
        "video": rl.catalog_prompt("pov-seasonal", "christmas-pov", 45)[0],
        "strength": 0.7, "hold_exposure": True, "trim_dash": True, "spec": "christmas", "clean": False},
    "xmas-drive-reindeer": {
        "still": 'Portrait photograph taken from the front passenger seat of a car on an empty quiet residential street, the camera held up close to the clean windshield and aimed forward and slightly right, so the street and houses fill the whole picture and only the very top edge of the dark dashboard shows as a thin line along the bottom, in light snowfall after dark, a glowing reindeer and candy-cane path markers on the lawn at the right, and the next two houses with white icicle lights, red wreaths and a brightly lit spruce between them, snow along the kerbs, damp asphalt, photorealistic',
        "video": rl.catalog_prompt("pov-seasonal", "christmas-pov", 50)[0],
        "strength": 0.7, "hold_exposure": True, "trim_dash": True, "spec": "christmas", "clean": False},
    "xmas-drive-lake": {
        "still": 'Portrait photograph taken from the front passenger seat of a car on an empty quiet residential street, the camera held up close to the clean windshield and aimed forward and slightly right, so the street and houses fill the whole picture and only the very top edge of the dark dashboard shows as a thin line along the bottom, at dusk on a narrow lakefront neighborhood road, snowy cottages on the right with old-fashioned colored bulbs, illuminated wreaths and little fir trees with warm lights, a stone mailbox and two small lit shrubs close by, snow along the kerbs, damp asphalt, photorealistic',
        "video": rl.catalog_prompt("pov-seasonal", "christmas-pov", 55)[0],
        "strength": 0.7, "hold_exposure": True, "trim_dash": True, "spec": "christmas", "clean": False},
    "xmas-alpine-a": {
        "still": 'Portrait photograph taken from an empty snowy hillside terrace above a Swiss Alpine village at deep blue hour, looking slightly downhill, one close snow-covered chalet roof at the bottom edge, dozens of steep snowy chalet roofs stepping down the slope and filling the lower two-thirds of the picture, dense warm amber windows, a few Christmas lights along the balconies, one especially bright cluster of lodges near centre-left, a dark pine-covered ridge rising to the right behind the village, a distant snow-covered summit at the upper left, clear cobalt sky, fine snow falling, photorealistic',
        "video": rl.catalog_prompt("pov-seasonal", "christmas-pov", 60)[0],
        "strength": 0.5, "hold_exposure": True, "spec": "christmas", "clean": False},
    "xmas-alpine-b": {
        "still": 'Portrait photograph taken from an empty snowy hillside terrace above a Swiss Alpine village at deep blue hour, looking slightly downhill, one close snow-covered chalet roof at the bottom edge, steep snowy chalet roofs stepping down the slope, golden hotel windows, red bows and warm-white Christmas strings tracing a winding main lane far below, a single lit outdoor tree at the bend, a dark pine ridge to the right and a distant Matterhorn-like summit beyond the rooftops, clear cobalt sky, fine snow falling, photorealistic',
        "video": rl.catalog_prompt("pov-seasonal", "christmas-pov", 65)[0],
        "strength": 0.5, "hold_exposure": True, "spec": "christmas", "clean": False},
    # The Lab's other two Alpine overlooks.
    "xmas-alpine-c": {
        "still": 'Portrait photograph taken from an empty snowy hillside terrace above a Swiss Alpine village at deep blue hour, looking slightly downhill, one close snow-covered chalet roof at the bottom edge, staggered rows of snowy timber homes descending toward a small steeple and a lit square, warm-white balcony lights, small wreaths and pools of amber window light on each tier, the snow sloping toward a shadowy valley on the left, distant peaks beyond, clear cobalt sky, fine snow falling, photorealistic',
        "video": rl.catalog_prompt("pov-seasonal", "christmas-pov", 70)[0],
        "strength": 0.5, "hold_exposure": True, "spec": "christmas", "clean": False},
    "xmas-alpine-d": {
        "still": 'Portrait photograph taken from an empty snowy hillside terrace above a Swiss Alpine village at deep blue hour, looking slightly downhill, one close snow-covered chalet roof at the bottom edge, two warmly lit clusters of snowy chalets separated by a white meadow, golden light strings outlining the near balconies, a tall outdoor spruce with warm-white lights in the far cluster, a dark pine ridge and distant peaks beyond, clear cobalt sky, fine snow falling, photorealistic',
        "video": rl.catalog_prompt("pov-seasonal", "christmas-pov", 75)[0],
        "strength": 0.5, "hold_exposure": True, "spec": "christmas", "clean": False},
    # Silhouette-truck format: the Lab's vertical still of a couple in silhouette beside a truck. Stills
    # only (run with --stage stills); the still prompt is the Lab's own, read from its catalog.
    "silhouette-dusk": {
        "still": rl.catalog_prompt("silhouette-truck", "silhouette", 0)[0],
        "video": "", "strength": 0.8, "spec": "silhouette", "clean": False},
    "silhouette-wheat": {
        "still": rl.catalog_prompt("silhouette-truck", "silhouette", 17)[0],
        "video": "", "strength": 0.8, "spec": "silhouette", "clean": False},
    # The same two shots rendered backwards. Forwards (above) the pinned first frame is only water,
    # so the skyline came from the depth boxes alone and was drawn as glowing box outlines
    # (harbor-night b1, 1/5 slices). Backwards, the first frame is a photographed-looking skyline
    # from the look step, the camera ends on the water, and the clip is reversed afterwards. The
    # video prompt therefore describes the move as it is rendered, skyline first.
    "harbor-night-rev": {
        # no "phone" in the still prompts: with it, the dusk still grew a phone on a mount on the bow
        "still": "Photograph taken from onboard a speedboat at night in a big city harbor, the plain white bow low in "
                 "frame, moody dark choppy open water reflecting a dense skyline of real glass skyscrapers lit up at "
                 "night, window lights and bright fluorescent light strips on the towers, hazy night sky, raw "
                 "handheld look, deep blacks, photorealistic",
        "video": "POV Tik Tok style video from onboard a speedboat at night in the harbor of Shanghai, a handheld semi "
                 "shaky view of the city skyline lit up at night, the towers' bright fluorescent lights reflected in "
                 "the moody dark waters, cinematic raw vibes, then the camera tilts quickly down to a view of dark "
                 "choppy open water and white wake foam rushing past the side of the boat",
        # end_still: a water picture pinned on the last rendered frame, which is the clip's opening
        # once reversed. water_end_sharp.png is a sharp look still of wake and water from round b1;
        # water_end.png, a frame lifted from a b3 clip, was already motion-smeared and the frames
        # after it smeared to white (round b6).
        "strength": 0.55, "guide": 0.6, "reverse": True, "end_still": "water_end_sharp.png", "spec": "boat",
        "clean": False},
    "harbor-dusk-rev": {
        "still": "Photograph taken from onboard a fast speedboat at dusk in a big city marina bay, the plain white bow "
                 "low in frame, moody open harbor water reflecting a skyline of real high-rise towers with glowing "
                 "window lights under a pastel dusk sky, raw handheld look, photorealistic",
        "video": "POV TikTok-style video from onboard a fast speedboat at dusk in Singapore Marina Bay, a semi-shaky "
                 "handheld view of the Singapore skyline, glowing high-rise tower lights and pastel dusk sky reflecting "
                 "on the moody harbor water, raw nightlife energy, then the camera does a quick whip-tilt down to dark "
                 "choppy open water and white wake foam rushing past the side of the boat, neon and sunset "
                 "reflections streaking across the waves, spray, intense speed, cinematic but realistic",
        # end_still: a water picture pinned on the last rendered frame, which is the clip's opening
        # once reversed. water_end_sharp.png is a sharp look still of wake and water from round b1;
        # water_end.png, a frame lifted from a b3 clip, was already motion-smeared and the frames
        # after it smeared to white (round b6).
        "strength": 0.55, "guide": 0.6, "reverse": True, "end_still": "water_end_sharp.png", "spec": "boat",
        "clean": False},
}


# The other three cities in the Lab's boat catalog (combinations 3, 4, 5), by the recipe that
# worked for the two above: move blocked backwards, skyline still pinned first, sharp water still
# pinned last, depth guide 0.6, clip reversed afterwards.
def _harbor_city(place: str, skyline: str) -> dict:
    # end_still: each city's own opening water picture, drawn by tools/blockout/city_water.py
    return {
        "still": f"Photograph taken from onboard a fast speedboat at dusk in {place}, the plain white bow low in "
                 f"frame, moody open harbor water reflecting {skyline}, glowing window lights under a pastel dusk "
                 "sky, raw handheld look, photorealistic",
        "video": f"POV TikTok-style video from onboard a fast speedboat at dusk in {place}, a semi-shaky handheld "
                 f"view of {skyline}, glowing high-rise tower lights and pastel dusk sky reflecting on the moody "
                 "harbor water, raw nightlife energy, then the camera does a quick whip-tilt down to dark choppy "
                 "open water and white wake foam rushing past the side of the boat, neon and sunset reflections "
                 "streaking across the waves, spray, intense speed, cinematic but realistic",
        "strength": 0.55, "guide": 0.6, "reverse": True, "end_still": "water_end_city.png", "spec": "boat",
        "clean": False}


SCENES["harbor-newyork-rev"] = _harbor_city(
    "New York Harbor", "the Lower Manhattan skyline of tall glass and stone skyscrapers")
SCENES["harbor-dubai-rev"] = _harbor_city(
    "Dubai Marina", "the Dubai Marina skyline of very tall slender residential towers standing close together")
SCENES["harbor-hongkong-rev"] = _harbor_city(
    "Hong Kong Victoria Harbour", "the Hong Kong Island skyline of dense high-rise towers below dark green hills")
SCENES["harbor-singapore-rev"] = _harbor_city(
    "Singapore Marina Bay", "the Singapore Marina Bay skyline of tall glass bank towers")
# Shanghai is the catalog's night scene: the night harbor's own prompts, with the city named in the still.
SCENES["harbor-shanghai-rev"] = {
    **SCENES["harbor-night-rev"],
    "still": "Photograph taken from onboard a speedboat at night on the Huangpu River in Shanghai, the plain white "
             "bow low in frame, moody dark choppy open water reflecting the Pudong skyline of real glass "
             "skyscrapers lit up at night, window lights and bright fluorescent light strips on the towers, hazy "
             "night sky, raw handheld look, deep blacks, photorealistic",
    "end_still": "water_end_city.png"}


# Model files each stage loads. ComfyUI maps them into memory, and from the external drive a
# mapped file that is not already in the Mac's file cache pages in at tens of MB/s: one look-still
# load took 33 minutes on 2026-11-22 while a plain read of the same drive ran at about 6 GB/s.
# Reading the files through once first puts them in the cache, and the load is then quick.
MODEL_ROOTS = [Path.home() / "ComfyUI/models", Path("/Volumes/OWC-Envoy-Ultra/smaths/models/comfyui-lookdev")]
STAGE_FILES = {
    "look": ["z_image_turbo_bf16.safetensors", "qwen_3_4b.safetensors", "ae.safetensors",
             "Z-Image-Turbo-Fun-Controlnet-Union.safetensors"],
    "clean": ["minimax_h3_image_pruned_bf16.safetensors", "minimax_h3_image_vae_fp16.safetensors",
              "qwen3vl_32b_minimax_h3_bf16.safetensors"],
    "video": ["ltx-2.3-22b-dev.safetensors", "gemma_3_12B_it.safetensors",
              "ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors",
              "ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors",
              "ltx-2.3-spatial-upscaler-x2-1.1.safetensors"],
}


COMFY_LOG = Path.home() / "ComfyUI/logs/comfyui.log"


def stage_files(stage: str) -> list[str]:
    found = []
    for name in STAGE_FILES[stage]:
        for root in MODEL_ROOTS:
            hits = glob.glob(str(root / "*" / name))
            if hits:
                found.append(hits[0])
                break
    return found


class warm:
    """Keep a stage's model files in the file cache while ComfyUI loads them.

        with warm("video"):
            ...queue the job and wait...

    One read-through is not enough: the cache was seen to drop most of a 75 GB pre-read within
    half an hour, before the load that needed it. So a thread re-reads the files (a pass over
    cached files costs seconds) until ComfyUI's log shows a sampler progress bar, which means
    the weights are in, or six minutes have gone, or the stage ends.
    """

    def __init__(self, stage: str):
        self.stage, self.files, self.stop = stage, stage_files(stage), threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def sampling(self, offset: int) -> bool:
        try:
            with COMFY_LOG.open("rb") as log:
                log.seek(offset)
                return b"it/s]" in (tail := log.read()) or b"s/it]" in tail
        except OSError:
            return False

    def run(self) -> None:
        offset = COMFY_LOG.stat().st_size if COMFY_LOG.exists() else 0
        started, passes = time.monotonic(), 0
        while self.files and not self.stop.is_set() and time.monotonic() - started < 360:
            with open("/dev/null", "wb") as sink:
                subprocess.run(["/bin/cat", *self.files], stdout=sink, check=False)
            passes += 1
            if self.sampling(offset):
                break
            self.stop.wait(5)
        say(f"  kept {len(self.files)}/{len(STAGE_FILES[self.stage])} {self.stage} model files cached: "
            f"{passes} passes in {time.monotonic() - started:.0f} s")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=120)


def say(text: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {text}", flush=True)


def sh(cmd: list[str]) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} {Path(cmd[1]).name if len(cmd) > 1 else ''} failed: "
                           f"{(done.stdout + done.stderr)[-400:]}")
    return done.stdout


def faults(path: Path, spec: dict, slices: bool = True) -> list[str] | None:
    """Faults of a still, on the whole frame and inside each of the five posted slices.

    A small door badge can pass the whole-frame look and still fail the slice it sits in
    (it cost the hilltop clip two slices), so the still that goes on is checked on the
    stricter slice view. None when the judge cannot answer. slices=False asks about the
    whole frame only: one judge call instead of six, for ranking candidates (the judge is a
    shared model and took 40-60 s a call when it was busy on 2026-11-22).
    """
    try:
        found = list(rl.still_faults(path, spec))
        if not slices:
            return found
        for piece in rl.crop_facts(path, 1, rl.vision_config(spec), spec.get("crops") or "both"):
            found += [f for f in piece["faults"] if f not in (spec.get("advisory") or []) and f != "truck_in_view"]
        return found
    except Exception as exc:  # noqa: BLE001 - the pipeline goes on without a judge
        say(f"  judge unavailable: {str(exc)[:80]}")
        return None


async def clean(src: Path, dest: Path, seed: int) -> None:
    width, height = h3_image._size(src)
    width, height = width // 32 * 32, height // 32 * 32          # the edit works on a x32 grid
    async with httpx.AsyncClient() as client:
        graph = h3_image.edit_graph(await h3_image.upload(client, src), h3_image.TRUCK_CLEAN, width, height, seed)
        await rl.prepare_server(client, graph)
        await h3_image.run_image(client, graph, dest)


def contact_sheet(candidates: list[Path], dest: Path) -> None:
    """The four look stills side by side, numbered, for choosing one by eye."""
    from PIL import Image, ImageDraw
    tiles = [Image.open(c).convert("RGB") for c in candidates]
    w = 672
    h = round(tiles[0].height * w / tiles[0].width)
    cols = 2
    rows = (len(tiles) + 1) // cols
    sheet = Image.new("RGB", (w * cols, h * rows))
    for i, tile in enumerate(tiles):
        x, y = (i % cols) * w, (i // cols) * h
        sheet.paste(tile.resize((w, h)), (x, y))
        draw = ImageDraw.Draw(sheet)
        draw.rectangle([x, y, x + 34, y + 34], fill=(0, 0, 0))
        draw.text((x + 12, y + 10), str(i + 1), fill=(255, 255, 255))
    sheet.save(dest, quality=88)


def choose(candidates: list[Path], spec: dict, force_pick: int) -> tuple[Path, list[str] | None]:
    """The look still to go on with, and its faults (None when the judge cannot answer)."""
    if force_pick:                                       # chosen by eye: the judge sees badges, not framing
        pick = candidates[force_pick - 1]
        return pick, faults(pick, spec)
    # rank the candidates on the whole frame, then look inside the slices, best-ranked first, and
    # stop at the first clean one: five judge calls for another candidate cost less than a cleaning
    # pass (hilltop v4: the first-ranked still had a badge in two slices while the fourth had none).
    judged = sorted(((c, faults(c, spec, slices=False)) for c in candidates),
                    key=lambda item: len(item[1]) if item[1] is not None else 99)
    pick, pick_faults = judged[0][0], None
    for candidate, whole in judged:
        if whole is None:
            break
        found = faults(candidate, spec)
        if found is not None and (pick_faults is None or len(found) < len(pick_faults)):
            pick, pick_faults = candidate, found
        if found == []:
            break
    return pick, pick_faults


def run_scene(scene: str, tag: str, do_clean: bool, spec: dict, seed: int, force_pick: int = 0,
              stills_from: str = "", stage: str = "all", force_clean: bool = False) -> dict:
    """One scene through the stages: stills -> (pick) -> clean -> video.

    stage "all" runs them in a row. Run by stage across many scenes instead (all the stills,
    then all the cleaning, then all the videos) and ComfyUI changes model twice, not three
    times a scene; each change costs minutes. The pick and the cleaned frame are kept in
    <name>_state.json between stages.
    """
    cfg = SCENES[scene]
    if cfg.get("spec"):                                  # a scene of another format is judged by that format's rules
        spec = json.loads((HERE.parent / "recipe_lab_specs" / f"{cfg['spec']}.json").read_text())
    if cfg.get("clean") is False:                        # the cleaning instruction is written for trucks
        do_clean = False
    shot = ROOT / scene
    name = f"{tag}-{scene}"
    row = {"scene": scene, "tag": tag, "stage": stage, "started": f"{datetime.now():%F %T}"}
    started = time.monotonic()
    state_file = shot / "hero" / f"{name}_state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    # stills_from: take the look stills of an earlier tag instead of making new ones
    pattern = str(shot / "hero" / f"{stills_from or tag}-{scene}_seed{seed}_*.png")

    if stage in ("all", "stills"):
        say(f"{scene}: blockout and depth pass")
        sh([str(HERE / "make_shot.sh"), scene])
        say(f"{scene}: look stills")
        if glob.glob(pattern):
            say(f"{scene}: reusing the look stills already made for seed {seed}")   # a re-run after a lost render
        else:
            with warm("look"):
                sh(["python3", str(EC / "lookdev.py"), str(shot), "--prompt", cfg["still"], "--strength",
                    str(cfg["strength"]), "--seed", str(seed), "--name", name])
    candidates = sorted(Path(p) for p in glob.glob(pattern))
    if not candidates:
        raise RuntimeError("no look stills for this scene: run the stills stage first")
    if stage == "stills":
        contact_sheet(candidates, shot / "hero" / f"{name}_sheet.jpg")
        for number, candidate in enumerate(candidates, 1):
            found = faults(candidate, spec)
            say(f"{scene}: still {number}: "
                f"{', '.join(sorted(set(found))) if found else 'no faults' if found == [] else 'unjudged'}")
        say(f"{scene}: stills ready, sheet {name}_sheet.jpg")
        return row

    if stage in ("all", "clean") or not state.get("pick"):
        pick, pick_faults = choose(candidates, spec, force_pick)
        say(f"{scene}: picked {pick.name} ("
            f"{', '.join(sorted(set(pick_faults))) if pick_faults else 'no faults' if pick_faults == [] else 'unjudged'})")
        state = {"pick": str(pick), "pick_faults": pick_faults, "frame": str(pick)}
        # Only clean a still the judge faulted. On one that was already clean the edit added small
        # round ornaments to the bumpers, so an unneeded pass costs four minutes and can do harm.
        # force_clean: the judge passed the still but the finished clip showed a small badge (canyon
        # rim's tailgate, the night lot's door, both 2026-11-22).
        marked = (force_clean or pick_faults is None
                  or any(f in ("no_emblem", "no_lettering") for f in pick_faults))
        if do_clean and marked and stage in ("all", "clean"):
            say(f"{scene}: cleaning emblems and lettering off the still")
            cleaned = shot / "hero" / f"{name}_clean.png"
            with warm("clean"):
                asyncio.run(clean(pick, cleaned, seed))
            clean_faults = faults(cleaned, spec)
            state["clean_still"], state["clean_faults"] = cleaned.name, clean_faults
            # keep the edit unless the judge finds it worse than what went in
            if force_clean or pick_faults is None or clean_faults is None or len(clean_faults) <= len(pick_faults):
                state["frame"] = str(cleaned)
            else:
                say(f"{scene}: cleaning made it worse ({sorted(set(clean_faults))}), keeping the original still")
        state_file.write_text(json.dumps(state))
    row["look_still"], row["look_faults"] = Path(state["pick"]).name, state.get("pick_faults")
    for key in ("clean_still", "clean_faults"):
        if key in state:
            row[key] = state[key]
    if stage == "clean":
        say(f"{scene}: frame ready ({Path(state['frame']).name})")
        return row

    say(f"{scene}: depth-guided video on LTX")
    guide = ["--strength", str(cfg["guide"])] if cfg.get("guide") else []   # how hard the depth video is held
    with warm("video"):
        if cfg.get("end_still"):                         # both ends pinned: tools/blockout/two_ends.py
            out = sh(["python3", str(HERE / "two_ends.py"), str(shot), "--ref", state["frame"],
                      "--end", str(shot / "hero" / cfg["end_still"]), "--name", name, "--seed", str(seed + 100),
                      "--prompt", cfg["video"], *guide])
        else:
            out = sh(["python3", str(EC / "run_shot.py"), str(shot), "--model", "ltx", "--ref", state["frame"],
                      "--name", name, "--seed", str(seed + 100), "--prompt", cfg["video"], *guide])
    match = re.search(r"^output (.+\.mp4)\s*$", out, re.M)
    if not match:
        raise RuntimeError(f"video step reported no file: {out[-300:]}")
    rendered = Path(match.group(1))                      # ComfyUI's output folder, on the internal disk
    if cfg.get("reverse"):                               # rendered end-first: turn it the right way round
        forward = rendered.with_name(rendered.stem + "_fwd.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(rendered), "-vf", "reverse", "-an",
                        "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(forward)], check=True)
        rendered = forward
    if cfg.get("hold_exposure"):                         # level out LTX's brightening over a long clip
        held = rendered.with_name(rendered.stem + "_held.mp4")
        sh([sys.executable, str(HERE / "hold_exposure.py"), str(rendered), str(held)])
        rendered = held
    if cfg.get("trim_dash"):                             # crop a tall dashboard down to a thin strip
        trimmed = rendered.with_name(rendered.stem + "_trim.mp4")
        sh([sys.executable, str(HERE / "trim_dash.py"), str(rendered), str(trimmed)])
        rendered = trimmed
    video = ROOT / "videos" / f"{scene}__{tag}.mp4"
    # The copy to the drive runs beside the scoring, not before it: on 2026-11-22 an open() on the
    # drive hung for 21 minutes (22:10-22:31) and held the whole batch at this line.
    copier = threading.Thread(target=lambda: (video.parent.mkdir(exist_ok=True), shutil.copy(rendered, video)),
                              daemon=True)
    copier.start()
    row["video"] = video.name
    try:
        facts, checks = rl.score(rendered, spec)
        row["checks"] = checks
        row["usable_slices"] = facts.get("usable_crops")
        row["slices"] = [c["faults"] for c in facts.get("crops", [])]
        row["brightness"], row["motion"] = facts["brightness"], facts["motion"]
    except Exception as exc:  # noqa: BLE001 - keep the clip unjudged
        row["judge_error"] = str(exc)[:200]
    copier.join(timeout=120)
    if copier.is_alive():
        row["video_at"] = str(rendered)
        say(f"{scene}: the drive is not taking the copy yet; the clip is at {rendered}")
    row["seconds"] = round(time.monotonic() - started)
    say(f"{scene}: done in {row['seconds']} s, usable slices {row.get('usable_slices', 'unjudged')}/5, "
        f"failed checks {[k for k, v in row.get('checks', {}).items() if not v]}")
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenes", nargs="+", choices=sorted(SCENES))
    parser.add_argument("--tag", default="v2")
    parser.add_argument("--no-clean", action="store_true")
    parser.add_argument("--seed", type=int, default=40)
    parser.add_argument("--stage", choices=["all", "stills", "clean", "video"], default="all",
                        help="run one stage for every scene named (stills, then clean, then video), or all in a row")
    parser.add_argument("--pick", type=int, default=0, help="use look still number N (1-4) instead of the judge's choice")
    parser.add_argument("--picks", default="", help="per-scene choices, e.g. barn=4,beach=2")
    parser.add_argument("--stills-from", default="", help="reuse the look stills made under this earlier tag")
    parser.add_argument("--force-clean", action="store_true", help="clean the picked still even if the judge passes it")
    args = parser.parse_args()
    picks = {k: int(v) for k, v in (item.split("=") for item in args.picks.split(",") if item)}
    spec = json.loads((HERE.parent / "recipe_lab_specs" / "truck.json").read_text())
    for scene in args.scenes:
        try:
            row = run_scene(scene, args.tag, not args.no_clean, spec, args.seed, picks.get(scene, args.pick),
                            args.stills_from, args.stage, args.force_clean)
        except Exception as exc:  # noqa: BLE001 - one scene failing must not stop the rest
            row = {"scene": scene, "tag": args.tag, "error": (str(exc) or repr(exc))[:400]}
            say(f"{scene}: FAILED: {row['error']}")
        if "video" in row or "error" in row:
            with (ROOT / "results.jsonl").open("a") as f:
                f.write(json.dumps(row) + "\n")
    say(f"blockout pipeline finished (stage {args.stage})")


if __name__ == "__main__":
    main()
