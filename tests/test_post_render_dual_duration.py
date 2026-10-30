"""Replay real 768p probe rounding through durable service admission, without encoding."""
import json
from pathlib import Path

import pytest

from services.ffmpeg import delivery_encode_args
from tests.test_post_render_dual_dedup import dual_render, payload
from tests.test_post_render_jobs import service

CASES = json.loads((Path(__file__).parent / 'fixtures/dual_duration_768p.json').read_text())['cases']


def measured_renderer(case, damage=None, *, exact=False):
    def renderer(source, output, request, *, clock_ms):
        dual_render(source, output, request, clock_ms=clock_ms)
        for side, directory in [('clean', output), ('burned', output / 'burned')]:
            probe = dict(case[side]['final_probe'])
            frames = case[side]['decoded_video_frames']
            if exact:
                probe['duration_ms'] = 1000
                # Preserve the old exact-duration path, including its existing
                # positive numeric frame-count vocabulary and unequal counts.
                frames = 30.0 if side == 'clean' else 29.0
            receipt_path = directory / 'receipt.json'
            receipt = json.loads(receipt_path.read_text())
            receipt['duration_ms'] = probe['duration_ms']
            decode_path = directory / 'decode.json'
            decode = json.loads(decode_path.read_text())
            decode.update(decoded_video_frames=frames, final_probe=probe)
            if damage:
                damage(side, receipt, decode)
            receipt_path.write_text(json.dumps(receipt))
            decode_path.write_text(json.dumps(decode))
    return renderer


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['name'])
def test_captured_one_ms_rounding_admits_and_survives_status_reload(tmp_path, case):
    worker = service(tmp_path, renderer=measured_renderer(case))
    job = worker.enqueue(payload('slot:rounding', 'rounding'), 'rounding-key')
    assert worker.run_one()
    assert worker.status(job['id'])['state'] == 'succeeded'
    # Reload validates the durable authority and receipt HMAC again.
    assert service(tmp_path).status(job['id'])['state'] == 'succeeded'


def test_fixed_delivery_contract_and_exact_duration_acceptance_are_preserved(tmp_path):
    argv = delivery_encode_args('tiktok_delivery_v1')
    assert argv[argv.index('-r') + 1] == '30'
    worker = service(tmp_path, renderer=measured_renderer(CASES[0], exact=True))
    job = worker.enqueue(payload('slot:exact', 'exact'), 'exact-key')
    assert worker.run_one()
    assert worker.status(job['id'])['state'] == 'succeeded'


@pytest.mark.parametrize('defect', [
    'different_frames', 'fractional_frames', 'boolean_frames', 'zero_frames',
    'two_ms', 'cross_ms_within_individual_bounds', 'both_off_cadence', 'rounded_center_only', 'nan_duration', 'infinite_duration',
    'boolean_duration', 'zero_duration', 'negative_duration', 'huge_duration', 'huge_frames_float_durations', 'clean_fractional_frames',
    'wrong_hash', 'wrong_qa', 'wrong_codec', 'wrong_dimensions',
])
def test_rounding_exception_cannot_admit_damaged_evidence(tmp_path, defect):
    def damage(side, receipt, decode):
        if defect == 'both_off_cadence':
            receipt['duration_ms'] = decode['final_probe']['duration_ms'] = 900 if side == 'clean' else 899
        if defect == 'cross_ms_within_individual_bounds' and side == 'clean':
            receipt['duration_ms'] = decode['final_probe']['duration_ms'] = 867.5
        if defect == 'rounded_center_only':
            receipt['duration_ms'] = decode['final_probe']['duration_ms'] = 868 if side == 'clean' else 867
        if defect == 'clean_fractional_frames' and side == 'clean':
            decode['decoded_video_frames'] = 26.0
        if defect == 'huge_frames_float_durations':
            decode['decoded_video_frames'] = 10 ** 400
            receipt['duration_ms'] = decode['final_probe']['duration_ms'] = 867.0 if side == 'clean' else 866.0
        if side != 'burned':
            return
        if defect == 'different_frames': decode['decoded_video_frames'] = 27
        if defect == 'fractional_frames': decode['decoded_video_frames'] = 26.0
        if defect == 'boolean_frames': decode['decoded_video_frames'] = True
        if defect == 'zero_frames': decode['decoded_video_frames'] = 0
        if defect == 'two_ms': decode['final_probe']['duration_ms'] = 865
        if defect == 'nan_duration': decode['final_probe']['duration_ms'] = float('nan')
        if defect == 'infinite_duration': decode['final_probe']['duration_ms'] = float('inf')
        if defect == 'boolean_duration': decode['final_probe']['duration_ms'] = True
        if defect == 'zero_duration': decode['final_probe']['duration_ms'] = 0
        if defect == 'negative_duration': decode['final_probe']['duration_ms'] = -1
        if defect == 'huge_duration': decode['final_probe']['duration_ms'] = 10 ** 400
        if defect == 'wrong_hash': decode['final_sha256'] = 'a' * 64
        if defect == 'wrong_qa': decode['qa_frame_sha256'] = 'b' * 64
        if defect == 'wrong_codec': decode['final_probe']['video_codec'] = 'hevc'
        if defect == 'wrong_dimensions': decode['final_probe']['height'] = 1080
    worker = service(tmp_path, renderer=measured_renderer(CASES[0], damage))
    job = worker.enqueue(payload('slot:damaged', 'damaged'), 'damaged-key')
    assert worker.run_one()
    state = worker.status(job['id'])
    assert state['state'] == 'failed'
    assert state['error_code'] in {'burned_fallback_decode_missing', 'output_authority_changed'}
