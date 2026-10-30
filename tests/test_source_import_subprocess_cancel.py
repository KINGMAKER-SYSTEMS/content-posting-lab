"""Import subprocesses cannot survive cancellation and write after cleanup."""
import asyncio
import sys
from pathlib import Path

import pytest

import routers.control_plane as cp
import scraper.frame_extractor as scraper
from services import control_plane_source_imports as imports


@pytest.mark.parametrize('kind', ['normalize', 'probe', 'thumbnail', 'download'])
@pytest.mark.asyncio
async def test_cancelled_import_child_is_killed_and_reaped_before_return(tmp_path, monkeypatch, kind):
    ready, gate, output = tmp_path / 'ready', tmp_path / 'gate', tmp_path / 'late-output'
    script = (
        'import pathlib,sys,time\n'
        'ready,gate,out=map(pathlib.Path,sys.argv[1:])\n'
        'ready.write_text("ready")\n'
        'while not gate.exists(): time.sleep(0.01)\n'
        'out.write_bytes(b"late")\n'
    )
    real_spawn = asyncio.create_subprocess_exec
    processes = []
    async def spawn(*_args, **_kwargs):
        if _args:
            assert _kwargs.get('start_new_session') is True
        process = await real_spawn(sys.executable, '-c', script, str(ready), str(gate), str(output),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        processes.append(process)
        return process
    if kind == 'download':
        process = await spawn()
        runner = asyncio.create_task(scraper._communicate_or_kill(process, True))
    else:
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        if kind == 'normalize':
            runner = asyncio.create_task(imports._normalize_video(tmp_path / 'source', tmp_path / 'dest'))
        elif kind == 'probe':
            runner = asyncio.create_task(imports._probe_video(tmp_path / 'source'))
        else:
            runner = asyncio.create_task(cp._thumbnail_manifest(tmp_path, tmp_path / 'source', 0))
    try:
        for _ in range(500):
            if ready.exists():
                break
            await asyncio.sleep(0.01)
        assert ready.exists(), 'child reached its delayed-open barrier'
        runner.cancel()
        await asyncio.sleep(0)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert processes[0].returncode is not None, 'the child exited before the caller returns'
        gate.touch()
        await asyncio.sleep(0.05)
        assert not output.exists(), 'a canceled old child never opens an output later'
    finally:
        gate.touch(exist_ok=True)
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.asyncio
async def test_download_join_survives_repeated_cancellation(tmp_path, monkeypatch):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def download(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            raise
    monkeypatch.setattr(imports, 'download_video', download)
    runner = asyncio.create_task(imports._download_with_workspace_limit('https://fixture.test/video', tmp_path / 'video'))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runner.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        runner.cancel()
        await asyncio.sleep(0)
        assert not runner.done(), 'the caller joins child cleanup before releasing its claim'
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await runner


@pytest.mark.asyncio
async def test_normalizer_timeout_still_names_timeout_and_reaps_child(tmp_path, monkeypatch):
    process_holder = []
    real_spawn = asyncio.create_subprocess_exec
    async def spawn(*_args, **kwargs):
        assert kwargs.get('start_new_session') is True
        process = await real_spawn(sys.executable, '-c', 'import time; time.sleep(30)',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        process_holder.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(imports, 'MAX_SOURCE_NORMALIZE_SECONDS', 0.1)
    with pytest.raises(imports.SourceImportError, match='normalization timed out'):
        await imports._normalize_video(tmp_path / 'source', tmp_path / 'dest')
    assert process_holder[0].returncode is not None
