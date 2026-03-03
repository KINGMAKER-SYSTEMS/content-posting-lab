import type React from 'react';
import { Badge } from '@/components/ui/badge';

export interface Tab {
  id: string;
  label: string;
  icon?: React.ReactNode;
  count?: number;
}

interface TabNavProps {
  tabs: Tab[];
  activeTab: string;
  onTabChange: (tabId: string) => void;
  className?: string;
}

export function TabNav({
  tabs,
  activeTab,
  onTabChange,
  className = '',
}: TabNavProps) {
  return (
    <div className={`border-b-2 border-border bg-card sticky top-0 z-50 ${className}`}>
      <nav className="-mb-px flex gap-1 px-4 max-w-7xl mx-auto" aria-label="Tabs">
        {tabs.map((tab) => {
          const isActive = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              onClick={() => onTabChange(tab.id)}
              className={`whitespace-nowrap py-3 px-4 border-b-2 font-bold text-sm transition-colors flex items-center ${
                isActive
                  ? 'border-primary text-primary'
                  : 'border-transparent text-muted-foreground hover:text-foreground hover:border-border'
              }`}
              aria-current={isActive ? 'page' : undefined}
            >
              {tab.icon && <span className="mr-2">{tab.icon}</span>}
              {tab.label}
              {tab.count !== undefined && (
                <Badge
                  variant={isActive ? 'default' : 'secondary'}
                  className="ml-2 text-[10px] px-1.5 py-0 shadow-none"
                >
                  {tab.count}
                </Badge>
              )}
            </button>
          );
        })}
      </nav>
    </div>
  );
}
