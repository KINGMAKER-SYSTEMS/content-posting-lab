import type React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { EditorBayPage } from './EditorBay';

vi.mock('@mui/material', async () => {
  const React = await import('react');
  const component = (tag: keyof React.JSX.IntrinsicElements = 'div') => {
    const MockComponent = ({ children, ...props }: { children?: React.ReactNode }) =>
      React.createElement(tag, props, children);
    MockComponent.displayName = `Mock${tag}`;
    return MockComponent;
  };
  const Button = ({ children, onClick, disabled }: { children?: React.ReactNode; onClick?: () => void; disabled?: boolean }) => (
    <button type="button" onClick={onClick} disabled={disabled}>{children}</button>
  );
  return {
    Box: component('div'),
    AppBar: component('div'),
    Toolbar: component('div'),
    Typography: component('span'),
    IconButton: Button,
    Slider: ({ value, onChange }: { value?: number; onChange?: (_event: unknown, value: number) => void }) => (
      <input type="range" value={value ?? 0} onChange={(event) => onChange?.(event, Number(event.currentTarget.value))} />
    ),
    Drawer: component('aside'),
    List: component('ul'),
    ListItem: component('li'),
    ListItemText: ({ primary }: { primary?: React.ReactNode }) => <span>{primary}</span>,
    TextField: ({ value, onChange, placeholder }: { value?: string; onChange?: (event: React.ChangeEvent<HTMLTextAreaElement>) => void; placeholder?: string }) => (
      <textarea value={value ?? ''} onChange={onChange} placeholder={placeholder} />
    ),
    Button,
    Chip: ({ label }: { label?: React.ReactNode }) => <span>{label}</span>,
    Divider: component('hr'),
    Tooltip: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    ToggleButton: Button,
    ToggleButtonGroup: component('div'),
    CircularProgress: () => <span>Loading</span>,
    Paper: component('div'),
  };
});

vi.mock('@mui/icons-material/PlayArrow', () => ({ default: () => <span>play</span> }));
vi.mock('@mui/icons-material/Pause', () => ({ default: () => <span>pause</span> }));
vi.mock('@mui/icons-material/Gesture', () => ({ default: () => <span>draw</span> }));
vi.mock('@mui/icons-material/RadioButtonUnchecked', () => ({ default: () => <span>circle</span> }));
vi.mock('@mui/icons-material/NorthEast', () => ({ default: () => <span>arrow</span> }));
vi.mock('@mui/icons-material/DeleteOutlined', () => ({ default: () => <span>delete</span> }));
vi.mock('@mui/icons-material/AddComment', () => ({ default: () => <span>comment</span> }));
vi.mock('@mui/icons-material/Undo', () => ({ default: () => <span>undo</span> }));
vi.mock('react-konva', () => ({
  Stage: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Layer: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Line: () => null,
  Circle: () => null,
  Arrow: () => null,
}));
vi.mock('wavesurfer.js', () => ({
  default: {
    create: () => ({
      setMuted: vi.fn(),
      on: vi.fn(),
      setTime: vi.fn(),
      destroy: vi.fn(),
    }),
  },
}));

const baseProject = {
  schema: 'editor-timeline/v1',
  projectId: 'demo_edit',
  title: 'Demo Edit',
  fps: 30,
  width: 1920,
  height: 1080,
  revision: 3,
  assets: {
    card: { id: 'card', type: 'image', src: '/agenticnews-assets/card.png', metadata: {} },
    vo: { id: 'vo', type: 'audio', src: '/agenticnews-assets/vo.wav', metadata: {} },
  },
  tracks: {
    graphics_1: { id: 'graphics_1', kind: 'graphics', name: 'Graphics', index: 20, locked: false },
    audio_1: { id: 'audio_1', kind: 'audio', name: 'Voice', index: 40, locked: false },
  },
  clips: {
    card_clip: {
      id: 'card_clip',
      assetId: 'card',
      trackId: 'graphics_1',
      kind: 'artifact',
      start: 1,
      duration: 2,
      sourceStart: 0,
      enabled: true,
      muted: false,
      volume: 1,
      transform: { x: 0.2, y: 0.3, scale: 1, opacity: 1 },
      effects: [],
      keyframes: [],
      metadata: {},
    },
    vo_clip: {
      id: 'vo_clip',
      assetId: 'vo',
      trackId: 'audio_1',
      kind: 'voiceover',
      start: 0,
      duration: 5,
      sourceStart: 0,
      enabled: true,
      muted: false,
      volume: 0.8,
      transform: { x: 0.5, y: 0.5, scale: 1, opacity: 1 },
      effects: [],
      keyframes: [],
      metadata: {},
    },
  },
  markers: {},
  notes: {},
  effects: {},
  keyframes: {},
  renderCache: {
    frames: {
      '1.00': { frame: '/tmp/frame.png', at: 1 },
    },
  },
  commandLog: [],
  createdAt: 1,
  updatedAt: 1,
};

function withMovedClip() {
  return {
    ...baseProject,
    revision: 4,
    clips: {
      ...baseProject.clips,
      card_clip: {
        ...baseProject.clips.card_clip,
        start: 1.5,
      },
    },
  };
}

function withNote() {
  return {
    ...baseProject,
    revision: 4,
    notes: {
      note_1: {
        id: 'note_1',
        target: { clipId: 'card_clip', time: 1, frame: 1 },
        text: 'Fix the card timing',
        suggestedCommand: {
          op: 'clip.move',
          payload: { clipId: 'card_clip', start: 1.5 },
        },
        metadata: {},
      },
    },
  };
}

describe('EditorBayPage v2 timeline', () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/api/agenticnews/editor-timelines/demo_edit') && !init?.method) {
      return jsonResponse(baseProject);
    }
    if (url.endsWith('/api/agenticnews/editor-timelines/demo_edit/commands')) {
      const body = JSON.parse(String(init?.body || '{}'));
      if (body.op === 'clip.move') return jsonResponse(withMovedClip());
      if (body.op === 'note.add') return jsonResponse(withNote());
      return jsonResponse({ ...baseProject, revision: baseProject.revision + 1 });
    }
    if (url.endsWith('/api/agenticnews/editor-render/demo_edit/frame')) {
      return jsonResponse({ backend: 'ffmpeg', frame: '/tmp/frame.png', at: 1 });
    }
    return jsonResponse({}, 404);
  });

  beforeEach(() => {
    window.history.pushState({}, '', '/editor/demo_edit');
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockClear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('loads the commanded timeline graph and renders tracks and clips', async () => {
    render(<EditorBayPage />);

    expect(await screen.findByText('Demo Edit')).toBeTruthy();
    expect(screen.getByText('rev 3')).toBeTruthy();
    expect(screen.getByText('Graphics')).toBeTruthy();
    expect(screen.getByText('Voice')).toBeTruthy();
    expect(screen.getByRole('button', { name: /card_clip/i })).toBeTruthy();
    expect(screen.getByRole('button', { name: /vo_clip/i })).toBeTruthy();

    expect(fetchMock).toHaveBeenCalledWith('/api/agenticnews/editor-timelines/demo_edit');
  });

  it('dispatches clip move commands instead of privately mutating the timeline', async () => {
    const user = userEvent.setup();
    render(<EditorBayPage />);

    await user.click(await screen.findByRole('button', { name: /card_clip/i }));
    await user.click(screen.getByRole('button', { name: /nudge clip later/i }));

    await waitFor(() => expect(screen.getByText('rev 4')).toBeTruthy());

    const commandCall = fetchMock.mock.calls.find(([url, init]) => (
      String(url).endsWith('/api/agenticnews/editor-timelines/demo_edit/commands')
      && JSON.parse(String((init as RequestInit).body)).op === 'clip.move'
    ));
    expect(commandCall).toBeTruthy();
    const command = JSON.parse(String((commandCall![1] as RequestInit).body));
    expect(command).toMatchObject({
      op: 'clip.move',
      actor: 'human',
      expectedRevision: 3,
      payload: { clipId: 'card_clip', start: 1.5 },
    });
  });

  it('attaches notes to an exact clip and timeline time through the command API', async () => {
    const user = userEvent.setup();
    render(<EditorBayPage />);

    await user.click(await screen.findByRole('button', { name: /card_clip/i }));
    await user.type(screen.getByLabelText(/note text/i), 'Fix the card timing');
    await user.click(screen.getByRole('button', { name: /add clip note/i }));

    await waitFor(() => expect(screen.getByText('Fix the card timing')).toBeTruthy());

    const commandCall = fetchMock.mock.calls.find(([url, init]) => (
      String(url).endsWith('/api/agenticnews/editor-timelines/demo_edit/commands')
      && JSON.parse(String((init as RequestInit).body)).op === 'note.add'
    ));
    expect(commandCall).toBeTruthy();
    const command = JSON.parse(String((commandCall![1] as RequestInit).body));
    expect(command).toMatchObject({
      op: 'note.add',
      actor: 'human',
      expectedRevision: 3,
      payload: {
        target: { clipId: 'card_clip', time: 1, frame: 1 },
        text: 'Fix the card timing',
      },
    });
    expect(command.payload.suggestedCommand).toMatchObject({
      op: 'clip.move',
      payload: { clipId: 'card_clip' },
    });
  });
});

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}
