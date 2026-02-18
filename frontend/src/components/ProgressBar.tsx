import React from 'react';

interface ProgressBarProps {
  value?: number; // 0-100, if undefined -> indeterminate
  label?: string;
  color?: 'blue' | 'green' | 'red' | 'yellow' | 'purple';
  showValue?: boolean;
  className?: string;
}

export const ProgressBar: React.FC<ProgressBarProps> = ({
  value,
  label,
  color = 'blue',
  showValue = false,
  className = '',
}) => {
  const isIndeterminate = value === undefined;
  const percentage = value ? Math.min(100, Math.max(0, value)) : 0;

  const colorClasses = {
    blue: 'bg-blue-600 dark:bg-blue-500',
    green: 'bg-green-600 dark:bg-green-500',
    red: 'bg-red-600 dark:bg-red-500',
    yellow: 'bg-yellow-500 dark:bg-yellow-400',
    purple: 'bg-purple-600 dark:bg-purple-500',
  };

  return (
    <div className={`w-full ${className}`}>
      {(label || showValue) && (
        <div className="flex justify-between mb-1">
          {label && (
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
              {label}
            </span>
          )}
          {showValue && !isIndeterminate && (
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
              {Math.round(percentage)}%
            </span>
          )}
        </div>
      )}
      <div className="w-full bg-gray-200 rounded-full h-2.5 dark:bg-gray-700 overflow-hidden">
        <div
          className={`h-2.5 rounded-full ${colorClasses[color]} ${
            isIndeterminate ? 'animate-progress-indeterminate w-full origin-left' : 'transition-all duration-300 ease-out'
          }`}
          style={!isIndeterminate ? { width: `${percentage}%` } : {}}
        ></div>
      </div>
    </div>
  );
};
