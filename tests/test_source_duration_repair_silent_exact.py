
"""Silent treated-parent and exact decoded-frame repair regressions, offline only."""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from services import source_duration_repair as repair
from tests.test_source_duration_repair import good_command

FFMPEG = '/opt/homebrew/bin/ffmpeg'
FFPROBE = '/opt/homebrew/bin/ffprobe'


@pytest.fixture(scope='module')
def silent_source(tmp_path_factory):
    root = tmp_path_factory.mktemp('silent-treated-parent')
    source = root / 'source.mp4'
    # Same media clock/size/frame count as Joy, no private source or provider call.
    subprocess.run([FFMPEG, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=0x102030:s=768x1344:r=24', '-frames:v', '226',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2',
                    '-pix_fmt', 'yuv420p', '-an', str(source)], check=True, timeout=90)
    return source


def command(source, start=0, end=7500):
    changes = dict(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                   source_trim_start_ms=start, source_trim_end_ms=end,
                   official_start_ms=0, official_end_ms=end,
                   tail_evidence_end_ms=end,
                   expected_output_duration_ms=end-start,
                   expected_output_frames=(end-start)*30//1000)
    if start:
        changes.update(opening_evidence_id='test-opening',
                       opening_evidence_sha256=good_command()['decoded_reference_sha256'],
                       opening_evidence_end_ms=start)
    return good_command(changes)


def render(source, destination, cmd=None):
    return repair.render_shorter(source, destination, cmd or command(source),
                                 ffmpeg_bin=FFMPEG, ffprobe_bin=FFPROBE)


def test_real_silent_parent_repairs_to_exact_225_frames(silent_source, tmp_path):
    receipt = render(silent_source, tmp_path/'render')
    assert receipt['source_duration_ms'] == 9417
    assert receipt['final_duration_ms'] == 7500
    assert receipt['final_container_duration_ms'] == 7500
    assert receipt['final_packet_counts'] == {'video': 225}
    assert receipt['decoded_video']['decoded_video_frames'] == 225
    assert receipt['decoded_video']['video_start_ms'] == 0
    assert receipt['decoded_video']['video_end_ms'] == 7500
    assert receipt['ffprobe_final']['fps_num'] == 30
    assert receipt['ffprobe_final']['fps_den'] == 1
    assert receipt['ffprobe_final']['audio_streams'] == 0
    assert receipt['decoded_audio'] == dict(decoded_sample_count=0,
        decoded_channels=0, decoded_sample_rate=None, decoded_duration_ms=None)
    assert receipt['source_sha256'] == hashlib.sha256(silent_source.read_bytes()).hexdigest()
    assert receipt['final_sha256'] != receipt['source_sha256']
    assert receipt['command_digest'] == repair.command_digest(command(silent_source))


def test_silent_facts_do_not_attempt_mandatory_audio_decode(silent_source):
    facts = repair.decoded_audio_facts(silent_source, FFMPEG)
    assert facts == dict(decoded_sample_count=0, decoded_channels=0,
                        decoded_sample_rate=None, decoded_duration_ms=None)


def test_real_audio_still_decodes_and_corruption_is_not_silence(tmp_path):
    source = tmp_path/'audio.mp4'
    subprocess.run([FFMPEG, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=black:s=64x64:r=30:d=1', '-f', 'lavfi', '-i',
        'sine=frequency=440:sample_rate=48000:duration=1', '-c:v', 'libx264',
        '-preset', 'ultrafast', '-threads', '2', '-c:a', 'aac', '-shortest', str(source)],
        check=True, timeout=90)
    facts = repair.decoded_audio_facts(source, FFMPEG, channels=1, sample_rate=48000)
    assert facts['decoded_sample_count'] >= 48000
    assert facts['decoded_sample_rate'] == 48000
    assert facts['decoded_channels'] == 1
    bad = tmp_path/'bad.mp4'
    bad.write_bytes(b'not a media file')
    with pytest.raises(repair.RepairError):
        repair.decoded_audio_facts(bad, FFMPEG, channels=1, sample_rate=48000)


@pytest.mark.parametrize('frames', [224, 226])
def test_real_under_and_over_frames_refused(silent_source, tmp_path, monkeypatch, frames):
    """A forged delivery helper cannot pass a short or long real decoded final."""
    # Remove the separately tested audio bug so it cannot mask frame acceptance.
    monkeypatch.setattr(repair, 'decoded_audio_facts', lambda *a, **k: dict(
        decoded_sample_count=0, decoded_channels=0, decoded_sample_rate=None,
        decoded_duration_ms=None))
    def wrong(source, out, **kwargs):
        subprocess.run([FFMPEG, '-nostdin', '-v', 'error', '-i', str(silent_source),
            '-vf', 'fps=30', '-frames:v', str(frames), '-an', '-c:v', 'libx264',
            '-preset', 'ultrafast', '-threads', '2', '-pix_fmt', 'yuv420p', str(out)],
            check=True, timeout=90)
        return repair.probe(out, FFPROBE)
    monkeypatch.setattr(repair, 'encode_shorter_final', wrong)
    destination = tmp_path/'render'
    with pytest.raises(repair.RepairError):
        render(silent_source, destination)
    assert not destination.exists()


def test_packet_count_cannot_substitute_for_decode(silent_source, tmp_path, monkeypatch):
    # Real 224 decoded frames; falsify only packet and header witnesses.
    monkeypatch.setattr(repair, 'decoded_audio_facts', lambda *a, **k: dict(
        decoded_sample_count=0, decoded_channels=0, decoded_sample_rate=None,
        decoded_duration_ms=None))
    real_packets = repair.packet_ends
    def wrong(source, out, **kwargs):
        subprocess.run([FFMPEG, '-nostdin', '-v', 'error', '-i', str(silent_source),
            '-vf', 'fps=30', '-frames:v', '224', '-an', '-c:v', 'libx264',
            '-preset', 'ultrafast', '-threads', '2', '-pix_fmt', 'yuv420p', str(out)],
            check=True, timeout=90)
        facts = repair.probe(out, FFPROBE)
        facts.update(duration_ms=7500, container_duration_ms=7500)
        return facts
    def packets(path, ffprobe_bin=None):
        facts = real_packets(path, ffprobe_bin)
        if path.name == 'final.mp4':
            facts['counts']['video'] = 225
            facts['ends_s']['video'] = 7.5
        return facts
    monkeypatch.setattr(repair, 'encode_shorter_final', wrong)
    monkeypatch.setattr(repair, 'packet_ends', packets)
    with pytest.raises(repair.RepairError):
        render(silent_source, tmp_path/'render')


def test_nonzero_start_is_compared_to_span(silent_source, tmp_path):
    receipt = render(silent_source, tmp_path/'render', command(silent_source, 1000, 7500))
    assert receipt['decoded_video']['decoded_video_frames'] == 195
    assert receipt['final_duration_ms'] == 6500
    assert receipt['decoded_video']['video_end_ms'] == 6500
