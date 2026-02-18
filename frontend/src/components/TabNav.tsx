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
    <div className={`border-b border-gray-200 dark:border-gray-700 ${className}`}>
      <nav className="-mb-px flex space-x-8" aria-label="Tabs">
        {tabs.map((tab) => {
          const isActive = activeTab === tab.id;
          
          if (variant === 'pills') {
            return (
              <button
                key={tab.id}
                onClick={() => onTabChange(tab.id)}
                className={`${
                  isActive
                    ? 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200'
                    : 'text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'
                } px-3 py-2 font-medium text-sm rounded-md transition-colors flex items-center`}
                aria-current={isActive ? 'page' : undefined}
              >
                {tab.icon && <span className="mr-2">{tab.icon}</span>}
                {tab.label}
                {tab.count !== undefined && (
                  <span
                    className={`ml-2 py-0.5 px-2 rounded-full text-xs ${
                      isActive
                        ? 'bg-blue-200 text-blue-800 dark:bg-blue-800 dark:text-blue-100'
                        : 'bg-gray-100 text-gray-900 dark:bg-gray-700 dark:text-gray-300'
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
                  ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                  : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300 dark:text-gray-400 dark:hover:text-gray-300 dark:hover:border-gray-600'
              } whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm transition-colors flex items-center`}
              aria-current={isActive ? 'page' : undefined}
            >
              {tab.icon && <span className="mr-2">{tab.icon}</span>}
              {tab.label}
              {tab.count !== undefined && (
                <span
                  className={`ml-2 py-0.5 px-2 rounded-full text-xs ${
                    isActive
                      ? 'bg-blue-100 text-blue-600 dark:bg-blue-900 dark:text-blue-300'
                      : 'bg-gray-100 text-gray-900 dark:bg-gray-700 dark:text-gray-300'
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
