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


SCENES = {"lakeside": lakeside, "mountain-pullout": mountain_pullout,
          "open-field": open_field, "desert-road": desert_road,
          "hilltop": hilltop, "salt-flat": salt_flat,
          "snow-field": snow_field, "beach": beach, "barn": barn}


def main():
    a = args()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    SCENES[a.scene](scene, a.frames)
    bpy.ops.wm.save_as_mainfile(filepath=a.out)
    print("TRUCK_SCENE_SAVED", a.scene, a.out)


main()
