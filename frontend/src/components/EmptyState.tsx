import { type ReactNode } from 'react';
import { Button } from '@/components/ui/button';

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

export function EmptyState({
  icon,
  title,
  message,
  description,
  action,
  className = '',
}: EmptyStateProps) {
  const displayMessage = message || description;
  return (
    <div className={`flex flex-col items-center justify-center p-8 text-center ${className}`}>
      {icon && <div className="mb-4 text-4xl text-muted-foreground">{icon}</div>}
      <h3 className="text-lg font-heading text-foreground mb-2">{title}</h3>
      {displayMessage && <p className="text-sm text-muted-foreground max-w-sm mb-6">{displayMessage}</p>}
      {action && (
        <Button onClick={action.onClick}>
          {action.label}
        </Button>
      )}
    </div>
  );
}
