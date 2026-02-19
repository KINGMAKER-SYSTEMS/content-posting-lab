import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ProjectsPage } from './Projects';
import { useWorkflowStore } from '../stores/workflowStore';

function jsonResponse(data: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => data,
    text: async () => JSON.stringify(data),
  } as Response;
}

const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input);
  const method = init?.method || 'GET';

  if (url.includes('/api/projects') && method === 'GET') {
    return jsonResponse({
      projects: [
        {
          name: 'quick-test',
          path: '/tmp/projects/quick-test',
          video_count: 2,
          caption_count: 1,
          burned_count: 1,
        },
      ],
    });
  }

  if (url.includes('/api/projects') && method === 'POST') {
    return jsonResponse(
      {
        project: {
          name: 'new-campaign',
          path: '/tmp/projects/new-campaign',
          video_count: 0,
          caption_count: 0,
          burned_count: 0,
        },
      },
      201,
    );
  }

  if (url.includes('/api/projects') && method === 'DELETE') {
    return jsonResponse({ deleted: true, name: 'quick-test' });
  }

  return jsonResponse({});
});

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockClear();
  window.localStorage.clear();
  useWorkflowStore.setState({
    activeProject: null,
    jobs: {},
    notifications: [],
    recentlyGeneratedVideos: [],
    recentlyScrapedCaptions: [],
    videoRunningCount: 0,
    captionJobActive: false,
    burnReadyCount: 0,
    burnSelection: { videoPaths: [], captionSource: null },
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('ProjectsPage', () => {
  it('renders projects and aggregate stats', async () => {
    render(<ProjectsPage />);

    await waitFor(() => {
      expect(screen.getByText('quick-test')).toBeTruthy();
    });
    expect(screen.getByText('Total Videos')).toBeTruthy();
    expect(screen.getByText('Total Captions')).toBeTruthy();
    expect(screen.getByText('Total Burned')).toBeTruthy();
  });

  it('shows empty-state when no projects are returned', async () => {
    fetchMock.mockImplementationOnce(async () => jsonResponse({ projects: [] }));

    render(<ProjectsPage />);

    await waitFor(() => {
      expect(screen.getByText('No projects yet')).toBeTruthy();
    });
  });

  it('opens create modal and submits create project request', async () => {
    render(<ProjectsPage />);

    await waitFor(() => {
      expect(screen.getByText('quick-test')).toBeTruthy();
    });

    fireEvent.click(screen.getByRole('button', { name: /New Project/i }));
    fireEvent.change(screen.getByLabelText('Project Name'), {
      target: { value: 'New Campaign' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create Project' }));

    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(
        ([url, init]) => String(url).includes('/api/projects') && (init?.method || 'GET') === 'POST',
      );
      expect(postCall).toBeTruthy();
    });
  });
});
