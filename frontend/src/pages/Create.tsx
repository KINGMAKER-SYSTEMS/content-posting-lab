import { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useWorkflowStore } from '../stores/workflowStore';
import { GeneratePage } from './Generate';
import { ClipperPage } from './Clipper';
import { RecreatePage } from './Recreate';
import { SlideshowPage } from './Slideshow';
import { Badge } from '@/components/ui/badge';

interface SubTab {
  path: string;
  label: string;
  badge?: string | number;
}

export function CreatePage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { videoRunningCount, recreateJobActive } = useWorkflowStore();

  const subTabs = useMemo<SubTab[]>(
    () => [
      { path: '/create', label: 'Generate', badge: videoRunningCount > 0 ? videoRunningCount : undefined },
      { path: '/create/clipper', label: 'Clipper' },
      { path: '/create/recreate', label: 'Recreate', badge: recreateJobActive ? 'LIVE' : undefined },
      { path: '/create/slideshow', label: 'Slideshow' },
    ],
    [recreateJobActive, videoRunningCount],
  );

  // Lazy mounting — sub-tabs only render once visited
  const [visited, setVisited] = useState<Set<string>>(new Set([location.pathname]));
  useEffect(() => {
    setVisited((prev) => {
      if (prev.has(location.pathname)) return prev;
      return new Set([...prev, location.pathname]);
    });
  }, [location.pathname]);

  return (
    <div>
      {/* Sub-tab nav */}
      <nav className="sticky top-[53px] z-30 border-b-2 border-border bg-card">
        <div className="mx-auto flex max-w-7xl gap-1 px-4">
          {subTabs.map((tab) => {
            const isActive = location.pathname === tab.path;
            return (
              <button
                key={tab.path}
                type="button"
                onClick={() => navigate(tab.path)}
                className={`relative flex items-center gap-2 px-4 py-2.5 text-sm font-bold transition-colors ${
                  isActive
                    ? 'text-primary border-b-[3px] border-primary -mb-[2px]'
                    : 'text-muted-foreground hover:text-foreground hover:bg-muted'
                }`}
              >
                {tab.label}
                {tab.badge !== undefined && (
                  <Badge
                    variant={tab.badge === 'LIVE' ? 'success' : isActive ? 'default' : 'secondary'}
                    className="text-[10px] px-1.5 py-0 shadow-none"
                  >
                    {tab.badge}
                  </Badge>
                )}
              </button>
            );
          })}
        </div>
      </nav>

      {/* CSS display toggling — components stay mounted once visited */}
      {visited.has('/create') && (
        <div style={{ display: location.pathname === '/create' ? 'block' : 'none' }}>
          <GeneratePage />
        </div>
      )}
      {visited.has('/create/clipper') && (
        <div style={{ display: location.pathname === '/create/clipper' ? 'block' : 'none' }}>
          <ClipperPage />
        </div>
      )}
      {visited.has('/create/recreate') && (
        <div style={{ display: location.pathname === '/create/recreate' ? 'block' : 'none' }}>
          <RecreatePage />
        </div>
      )}
      {visited.has('/create/slideshow') && (
        <div style={{ display: location.pathname === '/create/slideshow' ? 'block' : 'none' }}>
          <SlideshowPage />
        </div>
      )}
    </div>
  );
}
