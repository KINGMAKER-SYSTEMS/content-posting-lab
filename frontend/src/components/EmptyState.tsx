import React, { type ReactNode } from 'react';

interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  message?: string;
  description?: string;
  action?: {
    label: string;
    onClick: () => void;
  };
  className?: string;
}

export const EmptyState: React.FC<EmptyStateProps> = ({
  icon,
  title,
  message,
  description,
  action,
  className = '',
}) => {
  const displayMessage = message || description;
  return (
    <div className={`flex flex-col items-center justify-center p-8 text-center ${className}`}>
      {icon && <div className="mb-4 text-4xl text-gray-600">{icon}</div>}
      <h3 className="text-lg font-medium text-white mb-2">{title}</h3>
      {displayMessage && <p className="text-sm text-gray-400 max-w-sm mb-6">{displayMessage}</p>}
      {action && (
        <button
          onClick={action.onClick}
          className="btn btn-primary"
        >
          {action.label}
        </button>
      )}
    </div>
  );
};
