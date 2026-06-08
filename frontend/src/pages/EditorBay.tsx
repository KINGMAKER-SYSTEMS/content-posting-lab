/**
 * EditorBay — a review scratchpad for rendered episodes (NOT an NLE).
 *
 * The operator scrubs the rendered episode, sees the ATOMIC timeline layers (cards / VO / captions /
 * lower-thirds / b-roll / demos) as tracks, and drops edit notes rooted in visual + temporal context:
 *   - FRAME notes: pause → draw (circle/arrow/scribble) on the frozen frame + a comment.
 *   - PIN notes: click a track region (e.g. a number card at 9:03) → a pin + a comment.
 * Both land in one shared notes rail and POST to /api/agenticnews/editor/:epId/notes, which the
 * editors/factory read. Built with MUI + react-konva (frame draw) + wavesurfer (VO track) — all
 * portable to an OpenCut-style fork (same React/Zustand/Tailwind-adjacent stack).
 */
import { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import {
  Box, AppBar, Toolbar, Typography, IconButton, Slider, Drawer, List, ListItem,
  ListItemText, TextField, Button, Chip, Divider, Tooltip, ToggleButton,
  ToggleButtonGroup, CircularProgress, Paper,
} from '@mui/material';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import PauseIcon from '@mui/icons-material/Pause';
import GestureIcon from '@mui/icons-material/Gesture';
import RadioButtonUncheckedIcon from '@mui/icons-material/RadioButtonUnchecked';
import NorthEastIcon from '@mui/icons-material/NorthEast';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutlined';
import AddCommentIcon from '@mui/icons-material/AddComment';
import UndoIcon from '@mui/icons-material/Undo';
import { Stage, Layer, Line, Circle, Arrow } from 'react-konva';
import WaveSurfer from 'wavesurfer.js';
import { apiUrl, staticUrl } from '../lib/api';

type Shot = { src?: string; startSec?: number; durationSec?: number };
type Word = { w: string; s: number; e: number };
type LowerThird = { startSec: number; durationSec: number; headline?: string };
type Segment = {
  segmentId?: string; title?: string; durationSec?: number;
  shots?: Shot[]; wordTimestamps?: Word[]; lowerThirds?: LowerThird[];
};
type Timeline = { segments?: Segment[]; totalSec?: number; musicBed?: string };
type Note = {
  id?: string; t: number; kind: 'frame' | 'pin'; track?: string; text: string;
  frameImg?: string; shapes?: any[]; createdAt?: string;
};
type EditorData = {
  epId: string; title: string; stage: string; duration: number;
  videoUrl: string; timeline: Timeline; notes: Note[];
};

// atomic track lanes, in render order (top = closest to viewer)
const TRACKS = [
  { key: 'hook', label: 'Hook', color: '#ef4444', match: (s: string) => s.includes('hook') },
  { key: 'card', label: 'Cards', color: '#7fd2ff', match: (s: string) => /_(number|vs|quote|diagram)\b|v2sc/.test(s) && !s.includes('hook') },
  { key: 'demo', label: 'Demo', color: '#a78bfa', match: (s: string) => s.includes('demo') },
  { key: 'ui', label: 'B-roll / UI', color: '#34d399', match: (s: string) => s.includes('_ui') || s.includes('broll') || s.includes('_bg') },
  { key: 'lower', label: 'Lower-thirds', color: '#fbbf24', match: () => false }, // populated from lowerThirds[]
  { key: 'vo', label: 'VO / Captions', color: '#94a3b8', match: () => false }, // populated from wordTimestamps[]
] as const;

const fmt = (s: number) => {
  if (!isFinite(s) || s < 0) s = 0;
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
};

export function EditorBayPage() {
  const epId = useMemo(() => {
    const m = window.location.pathname.match(/^\/editor\/(.+)$/);
    return m ? decodeURIComponent(m[1]) : '';
  }, []);

  const [data, setData] = useState<EditorData | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const [playing, setPlaying] = useState(false);
  const [t, setT] = useState(0);
  const [duration, setDuration] = useState(0);
  const [notes, setNotes] = useState<Note[]>([]);
  const [draft, setDraft] = useState('');
  const [tool, setTool] = useState<'none' | 'draw' | 'circle' | 'arrow'>('none');
  const [shapes, setShapes] = useState<any[]>([]);
  const [drawing, setDrawing] = useState(false);

  const videoRef = useRef<HTMLVideoElement | null>(null);
  const stageWrapRef = useRef<HTMLDivElement | null>(null);
  const [stageSize, setStageSize] = useState({ w: 960, h: 540 });
  const waveRef = useRef<HTMLDivElement | null>(null);
  const wsRef = useRef<WaveSurfer | null>(null);

  // ---- load ----
  useEffect(() => {
    if (!epId) { setErr('No episode id in URL (expected /editor/<epId>)'); setLoading(false); return; }
    (async () => {
      try {
        const res = await fetch(apiUrl(`/api/agenticnews/editor/${epId}`));
        if (!res.ok) throw new Error(`load failed (${res.status})`);
        const d: EditorData = await res.json();
        setData(d);
        setNotes(d.notes || []);
        setDuration(d.duration || d.timeline?.totalSec || 0);
      } catch (e: any) {
        setErr(e?.message || 'failed to load');
      } finally {
        setLoading(false);
      }
    })();
  }, [epId]);

  // ---- video time sync ----
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    const onTime = () => setT(v.currentTime);
    const onMeta = () => setDuration(v.duration || duration);
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    v.addEventListener('timeupdate', onTime);
    v.addEventListener('loadedmetadata', onMeta);
    v.addEventListener('play', onPlay);
    v.addEventListener('pause', onPause);
    return () => {
      v.removeEventListener('timeupdate', onTime);
      v.removeEventListener('loadedmetadata', onMeta);
      v.removeEventListener('play', onPlay);
      v.removeEventListener('pause', onPause);
    };
  }, [data, duration]);

  // ---- responsive konva stage over the video ----
  useEffect(() => {
    const el = stageWrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      setStageSize({ w: r.width, h: r.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [data]);

  // ---- VO waveform (rendered from the FINAL episode mp4 audio, not the per-segment wavs, which are
  //      GC'd after the episode completes). Read-only visual reference; the <video> stays the player.
  useEffect(() => {
    if (!data || !waveRef.current) return;
    let ws: WaveSurfer | null = null;
    try {
      ws = WaveSurfer.create({
        container: waveRef.current,
        url: staticUrl(data.videoUrl),       // mp4 audio track
        height: 18,
        waveColor: '#3a4658',
        progressColor: '#94a3b8',
        cursorColor: 'transparent',
        interact: true,                       // click the waveform to seek
        normalize: true,
        barWidth: 1,
        barGap: 1,
      });
      ws.setMuted(true);                      // the <video> is the sound source; waveform is silent
      ws.on('interaction', (sec: number) => seekRef.current(sec));
      wsRef.current = ws;
    } catch { /* waveform is non-essential — never block the bay */ }
    return () => { try { ws?.destroy(); } catch {} wsRef.current = null; };
  }, [data]);

  // keep the waveform cursor in sync with the video without re-creating it
  useEffect(() => {
    const ws = wsRef.current;
    if (ws && duration) { try { ws.setTime(t); } catch {} }
  }, [t, duration]);

  const seek = useCallback((sec: number) => {
    const v = videoRef.current;
    if (v) { v.currentTime = Math.max(0, Math.min(sec, duration || sec)); setT(v.currentTime); }
  }, [duration]);
  const seekRef = useRef(seek);
  useEffect(() => { seekRef.current = seek; }, [seek]);

  const togglePlay = () => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) { setTool('none'); v.play(); } else v.pause();
  };

  // ---- konva drawing ----
  const annotating = tool !== 'none';
  useEffect(() => { if (annotating && videoRef.current) videoRef.current.pause(); }, [annotating]);

  const onStageDown = (e: any) => {
    if (!annotating) return;
    const pos = e.target.getStage().getPointerPosition();
    setDrawing(true);
    if (tool === 'draw') setShapes((s) => [...s, { type: 'line', points: [pos.x, pos.y] }]);
    else if (tool === 'circle') setShapes((s) => [...s, { type: 'circle', x: pos.x, y: pos.y, r: 2 }]);
    else if (tool === 'arrow') setShapes((s) => [...s, { type: 'arrow', points: [pos.x, pos.y, pos.x, pos.y] }]);
  };
  const onStageMove = (e: any) => {
    if (!drawing || !annotating) return;
    const pos = e.target.getStage().getPointerPosition();
    setShapes((s) => {
      const last = s[s.length - 1];
      if (!last) return s;
      const copy = s.slice(0, -1);
      if (last.type === 'line') copy.push({ ...last, points: [...last.points, pos.x, pos.y] });
      else if (last.type === 'circle') copy.push({ ...last, r: Math.hypot(pos.x - last.x, pos.y - last.y) });
      else if (last.type === 'arrow') copy.push({ ...last, points: [last.points[0], last.points[1], pos.x, pos.y] });
      return copy;
    });
  };
  const onStageUp = () => setDrawing(false);

  // capture the frozen frame + drawings as a data URL thumbnail
  const captureFrame = useCallback((): string | undefined => {
    const v = videoRef.current;
    if (!v) return undefined;
    try {
      const c = document.createElement('canvas');
      c.width = 480; c.height = Math.round(480 * (v.videoHeight / v.videoWidth || 0.5625));
      const ctx = c.getContext('2d');
      if (!ctx) return undefined;
      ctx.drawImage(v, 0, 0, c.width, c.height);
      // overlay the konva shapes scaled to the thumbnail
      const sx = c.width / stageSize.w, sy = c.height / stageSize.h;
      ctx.strokeStyle = '#ff3b3b'; ctx.lineWidth = 3; ctx.lineCap = 'round';
      for (const sh of shapes) {
        ctx.beginPath();
        if (sh.type === 'line' && sh.points.length >= 2) {
          ctx.moveTo(sh.points[0] * sx, sh.points[1] * sy);
          for (let i = 2; i < sh.points.length; i += 2) ctx.lineTo(sh.points[i] * sx, sh.points[i + 1] * sy);
        } else if (sh.type === 'circle') {
          ctx.arc(sh.x * sx, sh.y * sy, sh.r * sx, 0, Math.PI * 2);
        } else if (sh.type === 'arrow') {
          ctx.moveTo(sh.points[0] * sx, sh.points[1] * sy);
          ctx.lineTo(sh.points[2] * sx, sh.points[3] * sy);
        }
        ctx.stroke();
      }
      return c.toDataURL('image/jpeg', 0.7);
    } catch { return undefined; }
  }, [shapes, stageSize]);

  // ---- save a note ----
  const saveNote = async (kind: 'frame' | 'pin', track?: string) => {
    if (!data) return;
    const note: Note = {
      t: Math.round(t * 100) / 100,
      kind,
      track,
      text: draft.trim(),
      createdAt: new Date().toISOString(),
      ...(kind === 'frame' ? { frameImg: captureFrame(), shapes } : {}),
    };
    // optimistic
    setNotes((n) => [...n, note].sort((a, b) => a.t - b.t));
    setDraft(''); setShapes([]); setTool('none');
    try {
      const res = await fetch(apiUrl(`/api/agenticnews/editor/${data.epId}/notes`), {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(note),
      });
      const out = await res.json();
      if (out?.note?.id) setNotes((n) => n.map((x) => (x === note ? out.note : x)));
    } catch { /* keep optimistic */ }
  };

  const deleteNote = async (note: Note) => {
    setNotes((n) => n.filter((x) => x !== note));
    if (data && note.id) {
      try { await fetch(apiUrl(`/api/agenticnews/editor/${data.epId}/notes/${note.id}`), { method: 'DELETE' }); } catch {}
    }
  };

  if (loading) return <Box sx={{ display: 'grid', placeItems: 'center', height: '100vh', bgcolor: '#0b0e14' }}><CircularProgress /></Box>;
  if (err) return <Box sx={{ display: 'grid', placeItems: 'center', height: '100vh', bgcolor: '#0b0e14', color: '#fff' }}><Typography>{err}</Typography></Box>;
  if (!data) return null;

  const segs = data.timeline?.segments || [];
  // segment start offsets (cumulative) for absolute timeline placement
  let acc = 0;
  const segStarts = segs.map((s) => { const st = acc; acc += s.durationSec || 0; return st; });
  const total = duration || acc || 1;

  // build per-track absolute blocks
  const trackBlocks = TRACKS.map((tr) => {
    const blocks: { start: number; dur: number; label: string }[] = [];
    segs.forEach((seg, i) => {
      const base = segStarts[i];
      if (tr.key === 'lower') {
        (seg.lowerThirds || []).forEach((lt) => blocks.push({ start: base + lt.startSec, dur: lt.durationSec, label: lt.headline || '' }));
      } else if (tr.key === 'vo') {
        if (seg.durationSec) blocks.push({ start: base, dur: seg.durationSec, label: '' });
      } else {
        (seg.shots || []).forEach((sh) => {
          const src = sh.src || '';
          if (tr.match(src)) blocks.push({ start: base + (sh.startSec || 0), dur: sh.durationSec || 0, label: '' });
        });
      }
    });
    return { ...tr, blocks };
  });

  return (
    <Box sx={{ height: '100vh', display: 'flex', flexDirection: 'column', bgcolor: '#0b0e14', color: '#e7ecf3' }}>
      <AppBar position="static" sx={{ bgcolor: '#11151f', boxShadow: 'none', borderBottom: '1px solid #1e2433' }}>
        <Toolbar variant="dense">
          <Typography variant="subtitle1" sx={{ flexGrow: 1, fontWeight: 700 }}>
            Editor Bay · <span style={{ color: '#7fd2ff' }}>{data.title || data.epId}</span>
          </Typography>
          <Chip size="small" label={data.stage} sx={{ mr: 1, bgcolor: '#1e2433', color: '#9fb3c8' }} />
          <Chip size="small" label={`${fmt(total)} · ${notes.length} notes`} sx={{ bgcolor: '#1e2433', color: '#9fb3c8' }} />
        </Toolbar>
      </AppBar>

      <Box sx={{ display: 'flex', flex: 1, minHeight: 0 }}>
        {/* LEFT: player + draw overlay + scrubber + tracks */}
        <Box sx={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, p: 2, gap: 1.5 }}>
          <Box ref={stageWrapRef} sx={{ position: 'relative', bgcolor: '#000', borderRadius: 2, overflow: 'hidden', aspectRatio: '16/9', maxHeight: '52vh' }}>
            <video
              ref={videoRef}
              src={staticUrl(data.videoUrl)}
              style={{ width: '100%', height: '100%', display: 'block', objectFit: 'contain' }}
              onClick={() => !annotating && togglePlay()}
            />
            {annotating && (
              <Stage
                width={stageSize.w} height={stageSize.h}
                style={{ position: 'absolute', inset: 0, cursor: 'crosshair' }}
                onMouseDown={onStageDown} onMouseMove={onStageMove} onMouseUp={onStageUp}
                onTouchStart={onStageDown} onTouchMove={onStageMove} onTouchEnd={onStageUp}
              >
                <Layer>
                  {shapes.map((sh, i) => {
                    if (sh.type === 'line') return <Line key={i} points={sh.points} stroke="#ff3b3b" strokeWidth={4} lineCap="round" lineJoin="round" tension={0.4} />;
                    if (sh.type === 'circle') return <Circle key={i} x={sh.x} y={sh.y} radius={sh.r} stroke="#ff3b3b" strokeWidth={4} />;
                    if (sh.type === 'arrow') return <Arrow key={i} points={sh.points} stroke="#ff3b3b" fill="#ff3b3b" strokeWidth={4} pointerLength={14} pointerWidth={14} />;
                    return null;
                  })}
                </Layer>
              </Stage>
            )}
          </Box>

          {/* transport */}
          <Box sx={{ display: 'flex', flexDirection: 'row', gap: 1, alignItems: 'center' }}>
            <IconButton onClick={togglePlay} sx={{ color: '#7fd2ff' }}>{playing ? <PauseIcon /> : <PlayArrowIcon />}</IconButton>
            <Typography variant="caption" sx={{ width: 90, fontVariantNumeric: 'tabular-nums' }}>{fmt(t)} / {fmt(total)}</Typography>
            <Slider size="small" min={0} max={total} step={0.1} value={Math.min(t, total)}
              onChange={(_, v) => seek(v as number)}
              sx={{ color: '#7fd2ff', mx: 1 }} />
            <Divider orientation="vertical" flexItem sx={{ borderColor: '#1e2433' }} />
            <ToggleButtonGroup size="small" exclusive value={tool} onChange={(_, v) => setTool(v || 'none')}
              sx={{ '& .MuiToggleButton-root': { color: '#9fb3c8', borderColor: '#1e2433' }, '& .Mui-selected': { color: '#ff3b3b !important', bgcolor: '#1e2433 !important' } }}>
              <ToggleButton value="draw"><Tooltip title="Scribble"><GestureIcon fontSize="small" /></Tooltip></ToggleButton>
              <ToggleButton value="circle"><Tooltip title="Circle"><RadioButtonUncheckedIcon fontSize="small" /></Tooltip></ToggleButton>
              <ToggleButton value="arrow"><Tooltip title="Arrow"><NorthEastIcon fontSize="small" /></Tooltip></ToggleButton>
            </ToggleButtonGroup>
            {shapes.length > 0 && <IconButton size="small" onClick={() => setShapes((s) => s.slice(0, -1))} sx={{ color: '#9fb3c8' }}><UndoIcon fontSize="small" /></IconButton>}
          </Box>

          {/* ATOMIC TIMELINE TRACKS — click a block to pin a note at that time */}
          <Paper sx={{ bgcolor: '#11151f', p: 1, borderRadius: 2, overflowX: 'auto', flex: 1, minHeight: 0 }}>
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
              {trackBlocks.map((tr) => (
                <Box key={tr.key} sx={{ display: 'flex', alignItems: 'center', height: 26 }}>
                  <Typography variant="caption" sx={{ width: 92, flexShrink: 0, color: tr.color, fontWeight: 600 }}>{tr.label}</Typography>
                  <Box sx={{ position: 'relative', flex: 1, height: 18, bgcolor: '#0b0e14', borderRadius: 0.5 }}>
                    {/* VO lane = live waveform from the episode mp4 audio (click to seek) */}
                    {tr.key === 'vo' && <Box ref={waveRef} sx={{ position: 'absolute', inset: 0 }} />}
                    {tr.key !== 'vo' && tr.blocks.map((b, i) => (
                      <Tooltip key={i} title={`${tr.label} @ ${fmt(b.start)}${b.label ? ` — ${b.label}` : ''}`}>
                        <Box
                          onClick={() => { seek(b.start); setTool('none'); }}
                          sx={{
                            position: 'absolute', top: 0, height: '100%', borderRadius: 0.5, cursor: 'pointer',
                            left: `${(b.start / total) * 100}%`, width: `${Math.max(0.4, (b.dur / total) * 100)}%`,
                            bgcolor: tr.color, opacity: 0.55, '&:hover': { opacity: 1 },
                          }}
                        />
                      </Tooltip>
                    ))}
                  </Box>
                </Box>
              ))}
              {/* playhead */}
              <Box sx={{ position: 'relative', height: 0 }}>
                <Box sx={{ position: 'absolute', top: -((TRACKS.length) * 26 + 4), left: `calc(92px + ${(t / total) * 100}% * (100% - 92px) / 100%)`, width: 2, height: TRACKS.length * 26, bgcolor: '#fff', pointerEvents: 'none' }} />
              </Box>
            </Box>
          </Paper>
        </Box>

        {/* RIGHT: notes rail */}
        <Drawer variant="permanent" anchor="right" slotProps={{ paper: { sx: { width: 340, bgcolor: '#11151f', borderColor: '#1e2433', color: '#e7ecf3', position: 'relative' } } }} sx={{ width: 340, flexShrink: 0 }}>
          <Box sx={{ p: 1.5 }}>
            <Typography variant="subtitle2" sx={{ mb: 1, color: '#9fb3c8' }}>EDIT NOTES @ {fmt(t)}</Typography>
            <TextField
              fullWidth multiline minRows={2} size="small" placeholder="What needs fixing here? (rooted at this timestamp)"
              value={draft} onChange={(e) => setDraft(e.target.value)}
              sx={{ mb: 1, '& .MuiInputBase-root': { bgcolor: '#0b0e14', color: '#e7ecf3' }, '& fieldset': { borderColor: '#1e2433' } }}
            />
            <Box sx={{ display: 'flex', flexDirection: 'row', gap: 1 }}>
              <Button fullWidth size="small" variant="contained" startIcon={<AddCommentIcon />} disabled={!draft.trim()}
                onClick={() => saveNote('pin')} sx={{ bgcolor: '#7fd2ff', color: '#0b0e14', '&:hover': { bgcolor: '#5bc0f0' } }}>
                Pin note
              </Button>
              <Button fullWidth size="small" variant="outlined" startIcon={<GestureIcon />} disabled={!draft.trim() && shapes.length === 0}
                onClick={() => saveNote('frame')} sx={{ color: '#ff3b3b', borderColor: '#ff3b3b' }}>
                Frame note
              </Button>
            </Box>
            <Typography variant="caption" sx={{ color: '#5b6678', display: 'block', mt: 0.5 }}>
              Pin = timestamp + comment. Frame = pause, draw on the frame, then save.
            </Typography>
          </Box>
          <Divider sx={{ borderColor: '#1e2433' }} />
          <List dense sx={{ overflowY: 'auto' }}>
            {notes.length === 0 && <ListItem><ListItemText primary="No notes yet" sx={{ color: '#5b6678' }} /></ListItem>}
            {notes.map((n, i) => (
              <ListItem key={n.id || i} alignItems="flex-start"
                secondaryAction={<IconButton edge="end" size="small" onClick={() => deleteNote(n)} sx={{ color: '#5b6678' }}><DeleteOutlineIcon fontSize="small" /></IconButton>}
                sx={{ borderBottom: '1px solid #1e2433' }}>
                <Box sx={{ width: '100%' }}>
                  <Box sx={{ display: 'flex', flexDirection: 'row', gap: 1, alignItems: 'center', mb: 0.5 }}>
                    <Chip size="small" label={fmt(n.t)} onClick={() => seek(n.t)}
                      sx={{ bgcolor: '#0b0e14', color: '#7fd2ff', cursor: 'pointer', height: 20, fontVariantNumeric: 'tabular-nums' }} />
                    <Chip size="small" label={n.kind === 'frame' ? '✎ frame' : `📌 ${n.track || 'pin'}`}
                      sx={{ bgcolor: '#1e2433', color: n.kind === 'frame' ? '#ff3b3b' : '#9fb3c8', height: 20 }} />
                  </Box>
                  {n.frameImg && <img src={n.frameImg} alt="" style={{ width: '100%', borderRadius: 6, marginBottom: 6, cursor: 'pointer' }} onClick={() => seek(n.t)} />}
                  <Typography variant="body2" sx={{ color: '#e7ecf3', whiteSpace: 'pre-wrap' }}>{n.text}</Typography>
                </Box>
              </ListItem>
            ))}
          </List>
        </Drawer>
      </Box>
    </Box>
  );
}
