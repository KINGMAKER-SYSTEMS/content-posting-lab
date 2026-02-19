import React from 'react';

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
  variant?: 'underline' | 'pills';
}

export const TabNav: React.FC<TabNavProps> = ({
  tabs,
  activeTab,
  onTabChange,
  className = '',
  variant = 'underline',
}) => {
  return (
    <div className={`border-b border-white/10 bg-black/20 backdrop-blur-md sticky top-0 z-50 ${className}`}>
      <nav className="-mb-px flex space-x-8 px-4 max-w-7xl mx-auto" aria-label="Tabs">
        {tabs.map((tab) => {
          const isActive = activeTab === tab.id;
          
          if (variant === 'pills') {
            return (
              <button
                key={tab.id}
                onClick={() => onTabChange(tab.id)}
                className={`${
                  isActive
                    ? 'bg-purple-500/20 text-purple-300'
                    : 'text-gray-400 hover:text-gray-200 hover:bg-white/5'
                } px-3 py-2 font-medium text-sm rounded-md transition-colors flex items-center`}
                aria-current={isActive ? 'page' : undefined}
              >
                {tab.icon && <span className="mr-2">{tab.icon}</span>}
                {tab.label}
                {tab.count !== undefined && (
                  <span
                    className={`ml-2 py-0.5 px-2 rounded-full text-xs ${
                      isActive
                        ? 'bg-purple-500/20 text-purple-300'
                        : 'bg-white/10 text-gray-400'
                    }`}
                  >
                    {tab.count}
                  </span>
                )}
              </button>
            );
          }

          // Default underline variant
          return (
            <button
              key={tab.id}
              onClick={() => onTabChange(tab.id)}
              className={`${
                isActive
                  ? 'border-purple-500 text-purple-400'
                  : 'border-transparent text-gray-400 hover:text-gray-200 hover:border-gray-300'
              } whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm transition-colors flex items-center`}
              aria-current={isActive ? 'page' : undefined}
            >
              {tab.icon && <span className="mr-2">{tab.icon}</span>}
              {tab.label}
              {tab.count !== undefined && (
                <span
                  className={`ml-2 py-0.5 px-2 rounded-full text-xs ${
                    isActive
                      ? 'bg-purple-500/20 text-purple-300'
                      : 'bg-white/10 text-gray-400'
                  }`}
                >
                  {tab.count}
                </span>
              )}
            </button>
          );
        })}
      </nav>
    </div>
  );
};
