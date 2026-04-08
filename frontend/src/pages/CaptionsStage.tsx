import { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useWorkflowStore } from '../stores/workflowStore';
import { CaptionsPage } from './Captions';
import { BurnPage } from './Burn';
import { Badge } from '@/components/ui/badge';

interface SubTab {
  path: string;
  label: string;
  badge?: string | number;
}

export function CaptionsStagePage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { captionJobActive, burnReadyCount } = useWorkflowStore();

  const subTabs = useMemo<SubTab[]>(
    () => [
      { path: '/captions', label: 'Scrape', badge: captionJobActive ? 'LIVE' : undefined },
      { path: '/captions/burn', label: 'Burn', badge: burnReadyCount > 0 ? burnReadyCount : undefined },
    ],
    [burnReadyCount, captionJobActive],
  );

  // Lazy mounting
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

      {/* CSS display toggling */}
      {visited.has('/captions') && (
        <div style={{ display: location.pathname === '/captions' ? 'block' : 'none' }}>
          <CaptionsPage />
        </div>
      )}
      {visited.has('/captions/burn') && (
        <div style={{ display: location.pathname === '/captions/burn' ? 'block' : 'none' }}>
          <BurnPage />
        </div>
      )}
    </div>
  );
}
