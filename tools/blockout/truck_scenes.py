"""Build pickup-truck blockout scenes in Blender, for the blockout-to-video route.

A blockout is a scene made of plain boxes and cylinders. Its depth pass tells
the video model where everything is and how the camera moves, so the truck's
size, its place in the frame and the camera move are designed here instead of
being left to the model. (Depth export, hero still and video are ec's tools in
~/dev/ec/work/1121-blockout2video; this file only makes the .blend scenes.)

    blender -b -P tools/blockout/truck_scenes.py -- --scene lakeside --out /path/lakeside.blend [--frames 97]

Scenes follow the Lab's truck format: a parked full-size crew-cab pickup seen
from a low wheel-height camera, the whole truck and plenty of landscape in a
wide frame, with a slow camera move.
"""
import argparse
import math
import random
import sys

import bpy
import bmesh


def args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--scene", required=True, choices=sorted(SCENES))
    p.add_argument("--out", required=True)
    p.add_argument("--frames", type=int, default=97)
    return p.parse_args(argv)


def box(name, loc, half, parent=None):
    """A box centred at loc with half-extents (x, y, z)."""
    bpy.ops.mesh.primitive_cube_add(location=loc)
    o = bpy.context.object
    o.name, o.scale = name, half
    if parent:
        o.parent = parent
    return o


def truck(loc=(0, 0, 0), heading=0.0):
    """Full-size crew-cab pickup, about 5.9 m long, nose toward +Y before rotation.

    Proportions matter more than detail: the depth pass only carries shape, and
    the image model draws the truck that fits it. Raked windshield, separate
    hood, open bed and four wheels are enough to read as a pickup, not a van.
    """
    root = bpy.data.objects.new("truck", None)
    bpy.context.scene.collection.objects.link(root)
    box("body", (0, 0.0, 0.92), (1.0, 2.95, 0.37), root)           # sills up to the beltline
    box("hood", (0, 2.15, 1.36), (0.97, 0.8, 0.09), root)          # hood top
    cab = box("cab", (0, 0.35, 1.67), (0.94, 1.15, 0.30), root)    # greenhouse
    # rake the windshield and the back glass: pull the roof in at both ends
    bm = bmesh.new()
    bm.from_mesh(cab.data)
    for v in bm.verts:
        if v.co.z > 0:
            v.co.y *= 0.72 if v.co.y > 0 else 0.90
            v.co.x *= 0.90
    bm.to_mesh(cab.data)
    bm.free()
    for side in (-1, 1):                                            # bed walls
        box(f"bed_wall_{side}", (side * 0.95, -1.85, 1.42), (0.05, 1.05, 0.14), root)
    box("tailgate", (0, -2.92, 1.42), (1.0, 0.04, 0.14), root)
    box("bed_front", (0, -0.82, 1.42), (1.0, 0.04, 0.14), root)
    box("bumper_f", (0, 2.98, 0.62), (1.0, 0.08, 0.14), root)
    box("bumper_r", (0, -2.98, 0.62), (1.0, 0.08, 0.14), root)
    for x in (-0.92, 0.92):
        for y in (1.85, -1.85):
            bpy.ops.mesh.primitive_cylinder_add(radius=0.43, depth=0.32, vertices=40,
                                                location=(x, y, 0.43), rotation=(0, math.pi / 2, 0))
            w = bpy.context.object
            w.name, w.parent = "wheel", root
    root.location, root.rotation_euler = loc, (0, 0, heading)
    return root


def ground(size=600):
    bpy.ops.mesh.primitive_plane_add(size=size, location=(0, 0, 0))
    bpy.context.object.name = "ground"


def hill(name, loc, radius, height):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=1, location=loc, segments=48, ring_count=24)
    o = bpy.context.object
    o.name, o.scale = name, (radius, radius * 0.8, height)


def pine(loc, height):
    bpy.ops.mesh.primitive_cylinder_add(radius=0.18, depth=height * 0.3, location=(loc[0], loc[1], height * 0.15))
    bpy.ops.mesh.primitive_cone_add(radius1=height * 0.19, radius2=0.05, depth=height * 0.85,
                                    location=(loc[0], loc[1], height * 0.55))


def camera(scene, frames, start, end, look_at, lens=32):
    """Camera that keeps aiming at look_at while it eases from start to end."""
    bpy.ops.object.camera_add(location=start)
    cam = bpy.context.object
    cam.data.lens = lens
    cam.data.clip_end = 2000
    scene.camera = cam
    target = bpy.data.objects.new("cam_target", None)
    scene.collection.objects.link(target)
    target.location = look_at
    con = cam.constraints.new("TRACK_TO")
    con.target, con.track_axis, con.up_axis = target, "TRACK_NEGATIVE_Z", "UP_Y"
    scene.frame_start, scene.frame_end = 1, frames
    cam.keyframe_insert("location", frame=1)
    cam.location = end
    cam.keyframe_insert("location", frame=frames)
    return cam


# --- scenes --------------------------------------------------------------------
# The truck sits at the origin. The camera is at wheel height, about 11 m back,
# so the whole truck spans a little over half of a wide frame: every one of the
# Lab's five tall slices then has truck or landscape in it.

def lakeside(scene, frames):
    """Gravel shoulder beside a still pond, low grassy hillside across the water."""
    ground()
    truck(heading=math.radians(172))                               # nose toward the camera side: front three-quarter
    for i, (x, r, h) in enumerate([(-70, 55, 9), (10, 70, 13), (95, 60, 8)]):
        hill(f"far_hill_{i}", (x, 150, 0), r, h)                   # the far bank
    rnd = random.Random(4)
    for _ in range(26):                                            # reeds and tufts along the shore
        x, y = rnd.uniform(-45, 45), rnd.uniform(14, 22)
        bpy.ops.mesh.primitive_cone_add(radius1=0.35, radius2=0.02, depth=rnd.uniform(0.6, 1.3), location=(x, y, 0.4))
    camera(scene, frames, start=(5.0, -10.4, 0.55), end=(4.3, -9.4, 0.62), look_at=(0, 0.3, 1.15))


def mountain_pullout(scene, frames):
    """Mountain pullout: pines close behind, big peaks in the haze. Rear three-quarter."""
    ground()
    truck(heading=math.radians(8))                                 # nose away from the camera: rear three-quarter
    # No trees in the blockout. Cones stayed flat black triangles, wide rounded masses came out
    # as neat topiary and narrow ones as black cypress spikes: whatever shape is blocked, the
    # image model keeps it. Left empty, the depth pass just says "far" and the prompt's pine
    # forest is drawn naturally in the look still, which the video then holds as frame one.
    for i, (x, r, h) in enumerate([(-160, 120, 95), (40, 150, 130), (230, 130, 105)]):
        bpy.ops.mesh.primitive_cone_add(radius1=r, radius2=4, depth=h, vertices=9, location=(x, 420, h / 2))
        bpy.context.object.name = f"peak_{i}"
    box("guard_rail", (0, 9.5, 0.55), (40, 0.06, 0.2))
    camera(scene, frames, start=(-5.5, -9.8, 0.5), end=(-4.6, -9.0, 0.58), look_at=(0.8, 0.2, 1.15))


def open_field(scene, frames):
    """Farm track across an open field: fence line and a far tree line. Side profile, lateral drift."""
    ground()
    truck(heading=math.radians(-90))                               # nose to the right, broadside
    for i in range(-14, 15):                                       # fence posts and rails behind
        bpy.ops.mesh.primitive_cylinder_add(radius=0.09, depth=1.3, location=(i * 3.2, 11, 0.65))
    for z in (0.5, 1.0):
        box("rail", (0, 11, z), (46, 0.04, 0.05))
    rnd = random.Random(2)
    for i in range(40):                                            # distant tree line
        x = -200 + i * 10 + rnd.uniform(-3, 3)
        hill(f"tree_{i}", (x, 190, 6), rnd.uniform(6, 10), rnd.uniform(7, 12))
    camera(scene, frames, start=(-1.2, -11.5, 0.55), end=(1.0, -11.5, 0.55), look_at=(0, 0, 1.15), lens=35)


def desert_road(scene, frames):
    """Empty desert road with mesas on the horizon. Low front view, slow push-in."""
    ground()
    truck(heading=math.radians(-50))                               # front three-quarter
    box("road_edge_l", (-4.2, 60, 0.03), (0.12, 300, 0.03))
    box("road_edge_r", (4.2, 60, 0.03), (0.12, 300, 0.03))
    rnd = random.Random(6)
    for i, x in enumerate((-260, -120, 150, 300)):
        box(f"mesa_{i}", (x, 520 + rnd.uniform(-40, 40), 28), (rnd.uniform(40, 75), 40, rnd.uniform(22, 34)))
    for _ in range(30):                                            # scrub
        x, y = rnd.uniform(-70, 70), rnd.uniform(-10, 120)
        if abs(x) > 6:
            hill("scrub", (x, y, 0.2), rnd.uniform(0.5, 1.1), rnd.uniform(0.4, 0.8))
    camera(scene, frames, start=(3.3, 8.2, 0.5), end=(2.9, 7.3, 0.56), look_at=(0.7, 0.3, 1.15), lens=32)   # aimed off the truck's centre so it sits mid-frame and reaches both edge slices


def hilltop(scene, frames):
    """Truck on a rise with rolling hills falling away behind. Rear three-quarter, slow drift."""
    ground()
    truck(heading=math.radians(-10))
    for i, (x, y, r, h) in enumerate([(-120, 160, 90, 14), (60, 210, 120, 20), (220, 170, 80, 12), (-30, 320, 160, 30)]):
        hill(f"roll_{i}", (x, y, 0), r, h)
    camera(scene, frames, start=(5.2, -8.6, 0.55), end=(4.4, -8.0, 0.62), look_at=(-0.5, 0.2, 1.15))


def salt_flat(scene, frames):
    """Dead-flat empty plain, a thin mountain range far off. Side profile, slow push-in."""
    ground(size=2000)
    truck(heading=math.radians(90))                                # nose to the left, broadside
    for i in range(9):
        x = -640 + i * 160
        bpy.ops.mesh.primitive_cone_add(radius1=150, radius2=6, depth=60 + 25 * (i % 3), vertices=7,
                                        location=(x, 900, 30))
    camera(scene, frames, start=(0.4, -11.8, 0.55), end=(0.2, -10.2, 0.58), look_at=(0, 0, 1.15), lens=35)


def snow_field(scene, frames):
    """Snow-covered field in winter, low drifts, hills in the haze. Front three-quarter, slow push-in."""
    ground()
    truck(heading=math.radians(-165))                              # nose toward the camera side, from its other flank
    rnd = random.Random(11)
    for i in range(14):                                            # low drifts, kept well clear of the truck
        x, y = rnd.uniform(-60, 60), rnd.uniform(16, 70)
        hill(f"drift_{i}", (x, y, 0), rnd.uniform(4, 9), rnd.uniform(0.3, 0.7))
    for i, (x, r, h) in enumerate([(-110, 90, 16), (40, 120, 22), (190, 90, 14)]):
        hill(f"far_hill_{i}", (x, 260, 0), r, h)
    camera(scene, frames, start=(-5.0, -10.6, 0.55), end=(-4.4, -9.5, 0.6), look_at=(0, 0.3, 1.15))


def beach(scene, frames):
    """Flat wet sand at low tide, sea to the horizon, one headland far off. Low front-side view, slow drift."""
    ground(size=2000)
    truck(heading=math.radians(120))                               # nose to the left and a little toward the camera
    hill("headland", (-420, 600, 0), 260, 45)
    for i, y in enumerate((60, 90, 130)):                          # low swell lines, barely above the flat
        box(f"swell_{i}", (0, y, 0.12), (600, 1.2, 0.12))
    camera(scene, frames, start=(-2.6, -11.4, 0.5), end=(-1.4, -11.0, 0.55), look_at=(0, 0, 1.15), lens=35)


def barn(scene, frames):
    """Farmyard: a big gabled barn and a silo behind, open ground in front. Side profile, slow push-in."""
    ground()
    truck(heading=math.radians(90))                                # nose to the left, broadside
    # Barn back and to the right of the truck, gable end to the camera so it reads as a barn;
    # silo to the left, so the outer slices each have a building and the truck stays clear of both.
    bx, by, hw, hd, eave, ridge = 16.0, 44.0, 6.0, 8.5, 6.4, 10.2
    box("barn_body", (bx, by, eave / 2), (hw, hd, eave / 2))
    verts = [(bx - hw, by - hd, eave), (bx + hw, by - hd, eave), (bx, by - hd, ridge),
             (bx - hw, by + hd, eave), (bx + hw, by + hd, eave), (bx, by + hd, ridge)]
    faces = [(0, 1, 2), (5, 4, 3), (0, 2, 5, 3), (2, 1, 4, 5), (1, 0, 3, 4)]
    mesh = bpy.data.meshes.new("barn_roof")
    mesh.from_pydata(verts, [], faces)
    scene.collection.objects.link(bpy.data.objects.new("barn_roof", mesh))
    bpy.ops.mesh.primitive_cylinder_add(radius=2.2, depth=10, location=(-24, 52, 5))
    bpy.context.object.name = "silo"
    bpy.ops.mesh.primitive_uv_sphere_add(radius=2.2, location=(-24, 52, 10))
    for i in range(-10, 11):                                       # fence posts along the yard edge
        bpy.ops.mesh.primitive_cylinder_add(radius=0.09, depth=1.3, location=(i * 3.2, 16, 0.65))
    box("rail", (0, 16, 0.9), (33, 0.04, 0.05))
    camera(scene, frames, start=(0.5, -12.2, 0.55), end=(0.3, -10.8, 0.58), look_at=(0, 0, 1.3), lens=32)


def gable(scene, name, cx, cy, hw, hd, eave, ridge):
    """A building: box body with a gable roof, gable end facing the camera (-y)."""
    box(f"{name}_body", (cx, cy, eave / 2), (hw, hd, eave / 2))
    verts = [(cx - hw, cy - hd, eave), (cx + hw, cy - hd, eave), (cx, cy - hd, ridge),
             (cx - hw, cy + hd, eave), (cx + hw, cy + hd, eave), (cx, cy + hd, ridge)]
    faces = [(0, 1, 2), (5, 4, 3), (0, 2, 5, 3), (2, 1, 4, 5), (1, 0, 3, 4)]
    mesh = bpy.data.meshes.new(f"{name}_roof")
    mesh.from_pydata(verts, [], faces)
    scene.collection.objects.link(bpy.data.objects.new(f"{name}_roof", mesh))


def forest_road(scene, frames):
    """Dirt road through tall forest. Front three-quarter, slow push-in. Trees are left to the prompt."""
    ground()
    truck(heading=math.radians(172))
    for side in (-1, 1):                                           # low banks either side of the road
        box(f"bank_{side}", (side * 12.0, 60, 0.3), (2.5, 220, 0.3))
    # aimed a little to the truck's left so it sits right of where it was: at dead centre it stopped
    # 12 px short of the fifth slice (forest road s1, 4/5)
    camera(scene, frames, start=(4.8, -10.2, 0.5), end=(4.2, -9.3, 0.58), look_at=(-0.45, 0.1, 1.15))


def canyon_rim(scene, frames):
    """Parked at the edge of a canyon: the ground ends behind the truck, far wall across the gap. Rear three-quarter."""
    box("rim_ground", (0, -135, -0.5), (400, 165, 0.5))            # ground stops 30 m beyond the truck
    bpy.ops.mesh.primitive_plane_add(size=3000, location=(0, 500, -90))
    bpy.context.object.name = "canyon_floor"
    box("far_wall", (0, 330, -45), (900, 60, 45))                  # far rim level with this one
    for i, (x, y, hw, top) in enumerate([(-70, 150, 22, 4), (55, 190, 30, -6), (160, 130, 18, 8)]):
        box(f"butte_{i}", (x, y, (top - 90) / 2), (hw, hw * 0.8, (top + 90) / 2))
    truck(heading=math.radians(8))
    camera(scene, frames, start=(-5.5, -9.8, 0.5), end=(-4.7, -9.0, 0.58), look_at=(0.8, 0.2, 1.15))


def hay_field(scene, frames):
    """Mown hay field with round bales scattered behind. Side profile, slow sideways drift."""
    ground()
    truck(heading=math.radians(-90))                               # nose to the right, broadside
    rnd = random.Random(8)
    spots = [(-16, 14), (-6, 22), (9, 16), (19, 27), (-24, 34), (2, 40), (28, 48), (-12, 55), (14, 62), (-34, 70)]
    for i, (x, y) in enumerate(spots):
        end_on = rnd.random() < 0.4                                # some bales show their round end
        bpy.ops.mesh.primitive_cylinder_add(radius=0.8, depth=1.5, vertices=32, location=(x, y, 0.8),
                                            rotation=(math.pi / 2, 0, 0) if end_on else (0, math.pi / 2, 0))
        bpy.context.object.name = f"bale_{i}"
    for i, (x, r, h) in enumerate([(-120, 110, 10), (90, 140, 14)]):
        hill(f"far_rise_{i}", (x, 260, 0), r, h)
    camera(scene, frames, start=(1.2, -11.5, 0.55), end=(-1.0, -11.5, 0.55), look_at=(0, 0, 1.15), lens=35)


def cabin_snow(scene, frames):
    """Snowy clearing with a small log cabin behind. Front three-quarter, slow push-in. Pines left to the prompt."""
    ground()
    truck(heading=math.radians(-165))
    gable(scene, "cabin", 0.5, 29.0, 4.5, 3.5, 2.8, 5.0)       # left of the truck as the camera sees it
    box("porch", (0.5, 24.8, 0.25), (4.5, 0.8, 0.25))
    box("chimney", (3.0, 30.0, 5.4), (0.45, 0.45, 0.9))
    rnd = random.Random(3)
    for i in range(8):                                             # drifts, kept clear of truck and cabin
        x, y = rnd.uniform(14, 55), rnd.uniform(14, 60)
        hill(f"drift_{i}", (x, y, 0), rnd.uniform(3, 7), rnd.uniform(0.3, 0.6))
    camera(scene, frames, start=(-5.0, -10.6, 0.55), end=(-4.4, -9.5, 0.6), look_at=(0.6, 0.3, 1.25))


def lake_dock(scene, frames):
    """Gravel launch beside a lake, a long wooden dock running out over the water. Side profile, slow push-in."""
    ground(size=2000)
    truck(heading=math.radians(90))                                # nose to the left, broadside
    box("dock_deck", (9.5, 30, 0.75), (1.3, 17, 0.08))             # runs away from the camera, right of the truck
    for y in range(14, 48, 4):
        for x in (8.3, 10.7):
            bpy.ops.mesh.primitive_cylinder_add(radius=0.12, depth=1.7, location=(x, y, 0.85))
    for i, (x, r, h) in enumerate([(-200, 150, 22), (60, 190, 30), (300, 140, 18)]):
        hill(f"far_shore_{i}", (x, 420, 0), r, h)
    camera(scene, frames, start=(0.6, -12.0, 0.55), end=(0.4, -10.6, 0.58), look_at=(0.6, 0, 1.2), lens=32)


def night_lot(scene, frames):
    """Empty lot at night under one tall street lamp. Front three-quarter from the other side, slow push-in."""
    ground()
    truck(heading=math.radians(-50))
    bpy.ops.mesh.primitive_cylinder_add(radius=0.11, depth=9.0, location=(-6.0, -4.0, 4.5))
    bpy.context.object.name = "lamp_pole"
    box("lamp_arm", (-5.0, -4.0, 8.9), (1.1, 0.07, 0.07))
    box("lamp_head", (-4.0, -4.0, 8.8), (0.45, 0.18, 0.09))
    for i in range(-3, 4):                                         # kerb stops along the far edge of the lot
        box(f"stop_{i}", (i * 3.4 - 2.0, -16.0, 0.08), (0.9, 0.12, 0.08))
    box("kerb", (0, -24, 0.1), (60, 0.2, 0.1))
    camera(scene, frames, start=(3.3, 8.2, 0.5), end=(2.9, 7.3, 0.56), look_at=(0.7, 0.3, 1.15), lens=32)


# --- winter / Christmas set -------------------------------------------------------

def lying_tree(root_xy, length=2.6):
    """A cut Christmas tree lying in the truck bed, tip toward the tailgate. A cone stays a cone
    in the look still, which is exactly what a netted tree looks like."""
    x, y = root_xy
    bpy.ops.mesh.primitive_cone_add(radius1=0.55, radius2=0.03, depth=length, vertices=24,
                                    location=(x, y, 1.75), rotation=(math.pi / 2, 0, 0))
    return bpy.context.object


def tree_farm(scene, frames):
    """Christmas-tree farm in snow, a cut tree in the bed. Side profile, slow sideways drift.
    The rows of trees are left to the prompt; only the tree in the bed is blocked."""
    ground()
    root = truck(heading=math.radians(-90))                        # nose to the right, broadside
    tree = lying_tree((0, -1.75))                                  # over the bed, in truck space
    tree.parent = root
    for i, (x, r, h) in enumerate([(-140, 120, 12), (110, 150, 16)]):
        hill(f"far_rise_{i}", (x, 280, 0), r, h)
    camera(scene, frames, start=(-1.1, -11.5, 0.55), end=(1.1, -11.5, 0.55), look_at=(0, 0, 1.2), lens=35)


def covered_bridge(scene, frames):
    """Old covered bridge over a creek in snow, the truck parked on the approach. Front three-quarter."""
    ground()
    truck(heading=math.radians(172))
    gable(scene, "bridge", -1.0, 35.0, 3.2, 11.0, 4.2, 6.2)        # portal facing the camera, clear to the right of the truck in frame
    box("bridge_opening", (-1.0, 23.9, 1.9), (2.3, 0.15, 1.9))
    for side in (-1, 1):                                           # creek banks running across behind
        box(f"bank_{side}", (side * 30 - 1, 30, 0.35), (22, 3.0, 0.35))
    camera(scene, frames, start=(5.0, -10.4, 0.55), end=(4.3, -9.4, 0.62), look_at=(-0.6, 0.3, 1.3))


def frozen_lake(scene, frames):
    """Out on a frozen lake: dead-flat ice, a dark tree line far off. Rear three-quarter, slow drift."""
    ground(size=2000)
    truck(heading=math.radians(-10))
    box("shore", (0, 320, 4), (900, 20, 4))                        # a low dark shore line
    for i, (x, r, h) in enumerate([(-260, 170, 26), (120, 220, 34)]):
        hill(f"far_hill_{i}", (x, 520, 0), r, h)
    camera(scene, frames, start=(5.2, -8.6, 0.55), end=(4.4, -8.0, 0.62), look_at=(-0.5, 0.2, 1.15))


def cabin_lights(scene, frames):
    """The snowy cabin dressed for Christmas, with a tree standing outside. Front three-quarter, push-in."""
    ground()
    truck(heading=math.radians(-165))
    gable(scene, "cabin", 0.5, 29.0, 4.5, 3.5, 2.8, 5.0)
    box("porch", (0.5, 24.8, 0.25), (4.5, 0.8, 0.25))
    box("chimney", (3.0, 30.0, 5.4), (0.45, 0.45, 0.9))
    bpy.ops.mesh.primitive_cone_add(radius1=1.3, radius2=0.04, depth=3.6, vertices=24, location=(-6.0, 24.0, 1.9))
    bpy.context.object.name = "christmas_tree"                     # left of the cabin as the camera sees it
    camera(scene, frames, start=(-5.0, -10.6, 0.55), end=(-4.4, -9.5, 0.6), look_at=(0.6, 0.3, 1.25))


# --- boat format (the Lab's boat-lake: POV from a speedboat, water first, then the skyline) ----

def skyline(seed):
    """A city front about 300 m across the water: towers with set-backs, crowns and spires, low
    blocks between them. Plain boxes came back as glowing box outlines, so the shapes are varied."""
    rnd = random.Random(seed)
    box("quay", (0, 296, 1.5), (760, 6, 1.5))
    for i in range(46):                                            # low and mid-rise fill
        x, w = rnd.uniform(-620, 620), rnd.uniform(8, 20)
        h = rnd.uniform(14, 60)
        box(f"block_{i}", (x, rnd.uniform(305, 345), h / 2), (w, rnd.uniform(8, 16), h / 2))
    for i in range(26):                                            # towers
        x, y = rnd.uniform(-600, 600), rnd.uniform(330, 430)
        w, d = rnd.uniform(8, 17), rnd.uniform(8, 15)
        h = rnd.uniform(70, 170) if rnd.random() < 0.75 else rnd.uniform(190, 270)
        box(f"tower_{i}", (x, y, h / 2), (w, d, h / 2))
        top = h
        if rnd.random() < 0.7:                                     # a narrower upper section
            h2 = h * rnd.uniform(0.12, 0.3)
            box(f"setback_{i}", (x, y, top + h2 / 2), (w * 0.62, d * 0.62, h2 / 2))
            top += h2
        kind = rnd.random()
        if kind < 0.35:                                            # spire
            bpy.ops.mesh.primitive_cone_add(radius1=w * 0.28, radius2=0.2, depth=h * 0.22, vertices=8,
                                            location=(x, y, top + h * 0.11))
        elif kind < 0.55:                                          # rounded crown
            bpy.ops.mesh.primitive_uv_sphere_add(radius=w * 0.6, location=(x, y, top), segments=16, ring_count=8)


def harbor(scene, frames, towers_seed=5, speed=10.0, reverse=False):
    """Onboard a speedboat crossing a harbor at night: the camera starts low on the water just
    ahead of the bow, then whips up to a skyline across the bay. No truck in this one.

    The whole move is in the blockout: boat travel, the one-second hold on the water and the
    tilt-up. The skyline is boxes about 300 m off; this scene is exported with linear depth
    (make_shot.sh), because inverse depth leaves anything that far away black.
    """
    ground(size=6000)                                              # the water
    skyline(towers_seed)
    boat = bpy.data.objects.new("boat", None)
    scene.collection.objects.link(boat)
    scene.frame_start, scene.frame_end = 1, frames
    # reverse: the whole move runs backwards in time (skyline first, travelling astern, then down to
    # the water), so the lit skyline can be the pinned first frame; the clip is turned round in post.
    def at(frame):
        return frames + 1 - frame if reverse else frame

    travel = speed * (frames - 1) / 24.0
    boat.location = (0, 0, 0)
    boat.keyframe_insert("location", frame=at(1))
    boat.location = (0, travel, 0)
    boat.keyframe_insert("location", frame=at(frames))
    for fc in boat.animation_data.action.fcurves if hasattr(boat.animation_data.action, "fcurves") else []:
        for k in fc.keyframe_points:
            k.interpolation = "LINEAR"                             # steady speed, no ease in or out
    bpy.ops.mesh.primitive_cone_add(radius1=1.15, radius2=0.05, depth=3.4, vertices=20,
                                    location=(0, 2.3, 0.85), rotation=(-math.pi / 2, 0, 0))
    bow = bpy.context.object                                       # a plain pointed foredeck, tip forward
    bow.name, bow.scale, bow.parent = "bow", (1.0, 0.3, 1.0), boat
    # held out over the starboard side, so the opening is water with only the deck edge in a corner
    bpy.ops.object.camera_add(location=(1.25, 0.8, 1.6))
    cam = bpy.context.object
    cam.data.lens, cam.data.clip_end, cam.parent = 22, 4000, boat
    scene.camera = cam
    target = bpy.data.objects.new("cam_target", None)
    scene.collection.objects.link(target)
    target.parent = boat
    # steep enough at the start (about 40 degrees down) that the skyline is out of frame until the tilt
    for frame, loc in ((1, (2.3, 2.7, 0.0)), (26, (2.4, 2.9, 0.0)), (50, (0.0, 320.0, 55.0)), (frames, (0.0, 320.0, 62.0))):
        target.location = loc
        target.keyframe_insert("location", frame=at(frame))
    con = cam.constraints.new("TRACK_TO")
    con.target, con.track_axis, con.up_axis = target, "TRACK_NEGATIVE_Z", "UP_Y"


def harbor_dusk(scene, frames):
    harbor(scene, frames, towers_seed=9, speed=12.0)


# Half the first speed: at 10-12 m/s the opening water frames came out so motion-smeared that the
# judge read them as a broken render (night clips b2-b4).
def harbor_night_rev(scene, frames):
    harbor(scene, frames, towers_seed=5, speed=5.0, reverse=True)


def harbor_dusk_rev(scene, frames):
    harbor(scene, frames, towers_seed=9, speed=6.0, reverse=True)


# --- Christmas POV format (the Lab's pov-seasonal: an eight-second portrait walk at eye level) ----

def walk(scene, frames, length, look_at, eye=1.62, lens=24):
    """Eye-level camera already walking at a steady pace straight down +y, aimed at the destination.
    Keys are linear so the walk has no ease in or out; the footstep sway comes from the export's
    --shake (make_shot.sh)."""
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    camera(scene, frames, start=(0.15, 0.0, eye), end=(0.05, length, eye), look_at=look_at, lens=lens)


def xmas_lane(scene, frames):
    """Snow-dusted cobbled lane at blue hour: a stone doorway with a lantern close on the left,
    bay windows along both sides, a tall Christmas tree in the square at the end."""
    ground()
    rnd = random.Random(12)
    half = 2.3                                                     # half the lane's width
    for side in (-1, 1):
        for i in range(5):                                         # terraced houses, eaves to the lane
            h = rnd.uniform(6.5, 8.8)
            y = 3.6 + i * 7.3
            box(f"house_{side}_{i}", (side * (half + 3.2), y, h / 2), (3.2, 3.6, h / 2))
            if (i + (side > 0)) % 2 == 0:                          # a bay window pushing into the lane
                box(f"bay_{side}_{i}", (side * (half - 0.28), y + 0.8, 1.75), (0.3, 1.0, 0.75))
    # the doorway that passes close on the left: two stone jambs, a lintel, a lantern beside it
    # (8 m ahead at the start: a portrait frame is narrow, so anything nearer on the wall is out of
    # view; it reaches the frame edge about two seconds in and passes off it)
    for y in (7.6, 9.0):
        box("jamb", (-half + 0.14, y, 1.2), (0.16, 0.14, 1.2))
    box("lintel", (-half + 0.14, 8.3, 2.5), (0.2, 0.9, 0.14))
    box("lantern", (-half + 0.2, 9.65, 2.05), (0.11, 0.11, 0.18))
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.16, location=(-half + 0.1, 8.3, 1.75))   # the bow on the door
    # the square the lane opens into, and the tree that is the destination
    box("square_far_side", (0, 66, 4.8), (30, 3.0, 4.8))
    bpy.ops.mesh.primitive_cone_add(radius1=3.3, radius2=0.08, depth=11.5, vertices=28, location=(0, 45, 6.1))
    bpy.context.object.name = "christmas_tree"                      # 45 m off, 35 m by the end: it grows by a third
    bpy.ops.mesh.primitive_cylinder_add(radius=0.9, depth=0.5, location=(0, 45, 0.25))
    walk(scene, frames, length=9.6, look_at=(0, 45, 4.6))


def xmas_market(scene, frames):
    """Aisle between roofed Christmas stalls at night: a wreath passes close on the right, then a
    counter of wrapped boxes; the aisle opens toward a decorated tree in a snowy square."""
    ground()
    half = 1.8
    for side in (-1, 1):
        for i in range(6):
            y = 2.4 + i * 4.3
            cx = side * (half + 0.75)
            box(f"counter_{side}_{i}", (side * (half + 0.3), y, 0.5), (0.32, 1.8, 0.5))
            box(f"back_{side}_{i}", (side * (half + 1.45), y, 1.25), (0.08, 1.9, 1.25))
            for yy in (y - 1.8, y + 1.8):                           # posts at the stall's front corners
                bpy.ops.mesh.primitive_cylinder_add(radius=0.06, depth=2.4, location=(side * half, yy, 1.2))
            verts = [(cx - 1.1, y - 2.0, 2.4), (cx + 1.1, y - 2.0, 2.4), (cx, y - 2.0, 3.25),
                     (cx - 1.1, y + 2.0, 2.4), (cx + 1.1, y + 2.0, 2.4), (cx, y + 2.0, 3.25)]
            mesh = bpy.data.meshes.new(f"roof_{side}_{i}")
            mesh.from_pydata(verts, [], [(0, 1, 2), (5, 4, 3), (0, 2, 5, 3), (2, 1, 4, 5), (1, 0, 3, 4)])
            scene.collection.objects.link(bpy.data.objects.new(f"roof_{side}_{i}", mesh))
    bpy.ops.mesh.primitive_torus_add(major_radius=0.3, minor_radius=0.075, location=(half - 0.02, 6.7, 1.72),
                                     rotation=(0, math.pi / 2, 0))
    bpy.context.object.name = "wreath"                              # hung on the first post on the right
    rnd = random.Random(21)
    for i in range(7):                                              # wrapped boxes on the right-hand counter
        sz = rnd.uniform(0.11, 0.2)
        box(f"gift_{i}", (half + 0.3 + rnd.uniform(-0.12, 0.12), 9.4 + i * 0.42, 1.0 + sz), (sz, sz, sz))
    box("square_far_side", (0, 62, 4.5), (26, 3.0, 4.5))
    bpy.ops.mesh.primitive_cone_add(radius1=2.9, radius2=0.08, depth=9.5, vertices=28, location=(0, 42, 5.1))
    bpy.context.object.name = "christmas_tree"
    bpy.ops.mesh.primitive_cylinder_add(radius=0.8, depth=0.45, location=(0, 42, 0.22))
    walk(scene, frames, length=9.0, look_at=(0, 42, 3.9))


def xmas_bridge(scene, frames):
    """Inside a short covered timber footbridge: garlanded railings, a ribbon on a near post at the
    left, and through the far opening a row of timber shops and a tall spruce."""
    ground()
    half, start, length = 1.25, -1.0, 15.0
    box("deck", (0, start + length / 2, 0.06), (half, length / 2, 0.06))
    for side in (-1, 1):
        box(f"rail_{side}", (side * half, start + length / 2, 1.0), (0.05, length / 2, 0.06))
        box(f"kick_{side}", (side * half, start + length / 2, 0.45), (0.04, length / 2, 0.05))
        for i in range(7):                                         # posts carrying the roof
            box(f"post_{side}_{i}", (side * half, start + 0.6 + i * 2.3, 1.3), (0.07, 0.07, 1.3))
    verts = [(-half - 0.2, start, 2.6), (half + 0.2, start, 2.6), (0, start, 3.35),
             (-half - 0.2, start + length, 2.6), (half + 0.2, start + length, 2.6), (0, start + length, 3.35)]
    mesh = bpy.data.meshes.new("bridge_roof")
    mesh.from_pydata(verts, [], [(0, 2, 5, 3), (2, 1, 4, 5)])       # two roof slopes, open ends
    scene.collection.objects.link(bpy.data.objects.new("bridge_roof", mesh))
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.13, location=(-half + 0.02, 5.9, 1.25))   # the ribbon
    for i, x in enumerate((-13, -6.5, 0.5, 7.5, 14)):               # timber shops across the far end
        gable(scene, f"shop_{i}", x, 36.0, 3.0, 3.0, 3.6, 5.6)
    # No cone for the spruce: the look still puts its own trees among the shops, and a blocked cone
    # that disagreed with them came back as a pale ghost spire in the sky (bridge x1).
    walk(scene, frames, length=8.6, look_at=(1.2, 27, 2.6))


def xmas_bakery(scene, frames):
    """Stone pedestrian street toward a lit bakery on the corner: a glowing shop window close on the
    left, a fir-wrapped barrel on the right, gabled fronts, a small tree at the end."""
    ground()
    half = 2.9
    rnd = random.Random(15)
    for side in (-1, 1):
        for i in range(5):
            gable(scene, f"front_{side}_{i}", side * (half + 3.0), 3.4 + i * 6.4, 3.0, 3.1,
                  rnd.uniform(5.2, 6.6), rnd.uniform(7.6, 9.0))
    box("shop_window", (-half + 0.18, 8.6, 1.55), (0.22, 1.5, 0.95))    # the close window with the wreath
    bpy.ops.mesh.primitive_torus_add(major_radius=0.28, minor_radius=0.07, location=(-half + 0.42, 8.6, 1.9),
                                     rotation=(0, math.pi / 2, 0))
    bpy.ops.mesh.primitive_cylinder_add(radius=0.36, depth=0.95, location=(half - 0.55, 7.4, 0.48))
    bpy.context.object.name = "barrel"
    gable(scene, "bakery", -1.5, 41.0, 5.5, 3.2, 5.8, 8.6)              # the corner the street leads to
    box("bakery_window", (-1.5, 37.6, 1.6), (2.2, 0.2, 1.0))
    bpy.ops.mesh.primitive_cone_add(radius1=1.1, radius2=0.04, depth=3.6, vertices=20, location=(1.9, 33.5, 1.9))
    bpy.context.object.name = "small_tree"
    walk(scene, frames, length=8.4, look_at=(0, 36, 2.6))


def xmas_chapel(scene, frames):
    """Narrow snowy footpath to a small timber chapel: wreath over its door, a Christmas tree to
    one side, a lantern on a low stone wall passing close on the right."""
    ground()
    box("wall", (1.35, 14.0, 0.45), (0.22, 13.0, 0.45))             # low stone wall along the path
    box("lantern", (1.3, 6.4, 1.1), (0.12, 0.12, 0.2))
    gable(scene, "chapel", 0.0, 34.0, 3.4, 4.5, 4.2, 7.0)
    box("steeple", (0.0, 31.0, 8.2), (0.7, 0.7, 1.4))
    bpy.ops.mesh.primitive_cone_add(radius1=0.95, radius2=0.03, depth=3.2, vertices=4, location=(0, 31.0, 11.2),
                                    rotation=(0, 0, math.pi / 4))
    box("door", (0.0, 29.4, 1.15), (0.6, 0.12, 1.15))
    bpy.ops.mesh.primitive_torus_add(major_radius=0.3, minor_radius=0.07, location=(0, 29.35, 2.75),
                                     rotation=(math.pi / 2, 0, 0))
    bpy.ops.mesh.primitive_cone_add(radius1=1.6, radius2=0.05, depth=5.4, vertices=24, location=(-5.2, 28.0, 2.8))
    bpy.context.object.name = "christmas_tree"
    for i, (x, r, h) in enumerate([(-60, 45, 9), (55, 60, 12)]):
        hill(f"rise_{i}", (x, 110, 0), r, h)
    walk(scene, frames, length=8.4, look_at=(-0.4, 30, 3.0))


def xmas_mews(scene, frames):
    """Narrow mews of low redbrick homes, curving gently: a pine swag on the left, candlelit windows
    on the right, hanging stars and a lit potted spruce farther on."""
    ground()
    half = 2.0
    rnd = random.Random(27)

    def bend(y):                                                   # the lane drifts right as it goes
        return 0.006 * y * y

    for side in (-1, 1):
        for i in range(7):
            y = 3.0 + i * 5.6
            h = rnd.uniform(5.2, 6.4)
            box(f"home_{side}_{i}", (bend(y) + side * (half + 2.6), y, h / 2), (2.6, 2.75, h / 2))
            if side > 0 and i in (1, 2):                           # the pair of candlelit windows
                box(f"window_{i}", (bend(y) + half - 0.1, y - 0.4, 1.7), (0.12, 0.55, 0.7))
    bpy.ops.mesh.primitive_torus_add(major_radius=0.4, minor_radius=0.09, location=(-half + 0.1, 7.2, 2.0),
                                     rotation=(0, math.pi / 2, 0))
    bpy.context.object.name = "swag"
    for i in range(5):                                             # stars strung across the lane
        bpy.ops.mesh.primitive_ico_sphere_add(radius=0.2, subdivisions=1,
                                              location=(bend(22) - 1.4 + i * 0.7, 22.0, 3.9 - 0.12 * abs(i - 2)))
    bpy.ops.mesh.primitive_cylinder_add(radius=0.3, depth=0.5, location=(bend(26) + 1.2, 26.0, 0.25))
    bpy.ops.mesh.primitive_cone_add(radius1=0.75, radius2=0.03, depth=2.3, vertices=20, location=(bend(26) + 1.2, 26.0, 1.65))
    bpy.context.object.name = "potted_spruce"
    walk(scene, frames, length=8.4, look_at=(bend(24) + 0.3, 24, 2.2))


def xmas_drive(scene, frames):
    """From the front passenger seat of a car creeping along a snowy residential street at about
    7 mph, looking forward and slightly right: a close lawn slides by, the next decorated house and
    its tree are already in view. A narrow dark dashboard edge sits at the bottom of the frame."""
    ground()
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    box("kerb_r", (4.3, 40, 0.16), (0.35, 70, 0.16))               # snow-lined kerbs
    box("kerb_l", (-4.3, 40, 0.16), (0.35, 70, 0.16))
    gable(scene, "house_near", 14.0, 13.0, 5.0, 4.6, 5.0, 7.8)      # the lawn in front of it slides by
    box("porch_near", (8.4, 13.0, 0.45), (0.9, 2.2, 0.45))
    rnd = random.Random(33)
    for i in range(7):                                              # path markers / little lit things on the lawn
        bpy.ops.mesh.primitive_cylinder_add(radius=0.07, depth=0.95, location=(5.4 + rnd.uniform(0, 2.2), 6.0 + i * 1.4, 0.48))
    gable(scene, "house_next", 15.0, 35.0, 5.8, 5.0, 6.8, 10.2)     # two storeys, already visible
    box("porch_next", (8.6, 35.0, 0.5), (1.0, 2.6, 0.5))
    bpy.ops.mesh.primitive_cone_add(radius1=2.0, radius2=0.05, depth=6.4, vertices=24, location=(8.2, 26.5, 3.3))
    bpy.context.object.name = "yard_tree"
    gable(scene, "house_far", 15.0, 58.0, 5.4, 4.8, 5.4, 8.4)
    for i, y in enumerate((8.0, 30.0, 54.0)):                       # the other side of the street
        gable(scene, f"house_left_{i}", -15.0, y, 5.2, 4.6, 5.2, 8.0)
    car = bpy.data.objects.new("car", None)
    scene.collection.objects.link(car)
    scene.frame_start, scene.frame_end = 1, frames
    car.location = (2.4, 0, 0)
    car.keyframe_insert("location", frame=1)
    car.location = (2.4, 3.13 * (frames - 1) / 24.0, 0)             # 7 mph
    car.keyframe_insert("location", frame=frames)
    bpy.ops.object.camera_add(location=(0, 0, 1.22))
    cam = bpy.context.object
    cam.data.lens, cam.data.clip_end, cam.parent = 24, 2000, car
    scene.camera = cam
    target = bpy.data.objects.new("cam_target", None)               # rides with the car: a fixed aim, forward and a little right
    scene.collection.objects.link(target)
    target.parent, target.location = car, (9.0, 30.0, 2.6)          # far enough right that the houses fill the frame
    con = cam.constraints.new("TRACK_TO")
    con.target, con.track_axis, con.up_axis = target, "TRACK_NEGATIVE_Z", "UP_Y"
    dash = box("dashboard", (0.3, 1.15, 0.5), (1.6, 0.4, 0.05))
    dash.parent = car


def xmas_alpine(scene, frames):
    """Standing still on a snowy terrace above an Alpine village at blue hour: one close chalet roof
    at the bottom edge, dozens of roofs stepping down the slope, a pine ridge rising to the right and
    a far summit at the upper left. The camera only settles slightly."""
    slope = 0.24                                                    # metres of drop per metre away
    bpy.ops.mesh.primitive_plane_add(size=900, location=(0, 300, -slope * 300), rotation=(-math.atan(slope), 0, 0))
    bpy.context.object.name = "hillside"

    def chalet(name, cx, cy, hw, hd, wall, ridge, drop=0.0):
        z0 = -slope * cy - drop
        box(f"{name}_body", (cx, cy, z0 + wall / 2), (hw, hd, wall / 2))
        verts = [(cx - hw - 0.4, cy - hd - 0.4, z0 + wall), (cx + hw + 0.4, cy - hd - 0.4, z0 + wall), (cx, cy - hd - 0.4, z0 + ridge),
                 (cx - hw - 0.4, cy + hd + 0.4, z0 + wall), (cx + hw + 0.4, cy + hd + 0.4, z0 + wall), (cx, cy + hd + 0.4, z0 + ridge)]
        mesh = bpy.data.meshes.new(f"{name}_roof")
        mesh.from_pydata(verts, [], [(0, 1, 2), (5, 4, 3), (0, 2, 5, 3), (2, 1, 4, 5), (1, 0, 3, 4)])
        scene.collection.objects.link(bpy.data.objects.new(f"{name}_roof", mesh))

    # The terrace stands well above the slope, so the view looks down onto the roofs and they fill
    # the lower two-thirds of the frame. (From 1.65 m the bare slope filled the lower half instead.)
    eye = 9.5
    chalet("close", 1.5, 7.0, 5.0, 4.0, 3.0, 5.6, drop=-0.5)        # the roof that clips the bottom edge
    rnd = random.Random(44)
    for i in range(110):
        y = rnd.uniform(16, 190)
        x = rnd.uniform(-0.6 * y - 10, 0.55 * y + 10)
        chalet(f"chalet_{i}", x, y, rnd.uniform(3.2, 5.0), rnd.uniform(3.6, 5.4), rnd.uniform(3.0, 5.0), rnd.uniform(5.6, 8.2))
    hill("ridge", (250, 430, -110), 200, 150)                       # pine ridge rising to the right
    # no blocked summit: it is far beyond the depth range, so the prompt places it
    camera(scene, frames, start=(0, 0, eye), end=(0.02, 0.0, eye), look_at=(-2, 70, -22), lens=24)


# --- silhouette-truck format (the Lab's vertical still: a couple in silhouette beside a truck) ----

def silhouette_dusk(scene, frames):
    """A still, not a move: an embracing couple lower-left, the truck to their right, both small in
    the lower third of a portrait frame under a huge empty sky."""
    ground()
    truck(loc=(1.6, 0, 0), heading=math.radians(-90))              # broadside, nose to the right
    for i, x in enumerate((-4.25, -3.9)):                          # two figures standing together
        height = 1.78 - 0.13 * i
        bpy.ops.mesh.primitive_cylinder_add(radius=0.2, depth=height - 0.24, location=(x, 0.2, (height - 0.24) / 2))
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.12, location=(x + (0.06 if i == 0 else -0.06), 0.2, height - 0.12))
    camera(scene, frames, start=(-0.6, -22.0, 1.2), end=(-0.6, -21.98, 1.2), look_at=(-0.6, 0, 7.6), lens=35)


SCENES = {"lakeside": lakeside, "mountain-pullout": mountain_pullout,
          "open-field": open_field, "desert-road": desert_road,
          "hilltop": hilltop, "salt-flat": salt_flat,
          "snow-field": snow_field, "beach": beach, "barn": barn,
          "forest-road": forest_road, "canyon-rim": canyon_rim, "hay-field": hay_field,
          "cabin-snow": cabin_snow, "lake-dock": lake_dock, "night-lot": night_lot,
          "tree-farm": tree_farm, "covered-bridge": covered_bridge, "frozen-lake": frozen_lake,
          "cabin-lights": cabin_lights,
          # same blockouts, winter prompts
          "barn-snow": barn, "pass-snow": mountain_pullout,
          # boat format
          "harbor-night": harbor, "harbor-dusk": harbor_dusk,
          "harbor-night-rev": harbor_night_rev, "harbor-dusk-rev": harbor_dusk_rev,
          # Christmas POV format
          "xmas-lane": xmas_lane, "xmas-market": xmas_market, "xmas-bridge": xmas_bridge,
          "xmas-bakery": xmas_bakery, "xmas-chapel": xmas_chapel, "xmas-mews": xmas_mews,
          # one street blockout for the Lab's five suburban-drive scenes, one village for its overlooks
          "xmas-drive-candy": xmas_drive, "xmas-drive-icicle": xmas_drive, "xmas-drive-victorian": xmas_drive,
          "xmas-drive-reindeer": xmas_drive, "xmas-drive-lake": xmas_drive,
          "xmas-alpine-a": xmas_alpine, "xmas-alpine-b": xmas_alpine,
          "xmas-alpine-c": xmas_alpine, "xmas-alpine-d": xmas_alpine,
          # silhouette-truck format: stills only
          "silhouette-dusk": silhouette_dusk, "silhouette-wheat": silhouette_dusk}


def main():
    a = args()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    SCENES[a.scene](scene, a.frames)
    bpy.ops.wm.save_as_mainfile(filepath=a.out)
    print("TRUCK_SCENE_SAVED", a.scene, a.out)


main()
