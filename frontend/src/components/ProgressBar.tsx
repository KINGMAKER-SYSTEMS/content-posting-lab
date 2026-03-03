import { Progress } from '@/components/ui/progress';
import { cn } from '@/lib/utils';

interface ProgressBarProps {
  value?: number;
  label?: string;
  color?: 'primary' | 'success' | 'error' | 'warning' | 'accent';
  showValue?: boolean;
  className?: string;
}

const indicatorColors: Record<string, string> = {
  primary: '[&_[data-slot=progress-indicator]]:bg-primary',
  success: '[&_[data-slot=progress-indicator]]:bg-green-600',
  error: '[&_[data-slot=progress-indicator]]:bg-destructive',
  warning: '[&_[data-slot=progress-indicator]]:bg-amber-500',
  accent: '[&_[data-slot=progress-indicator]]:bg-accent',
};

export function ProgressBar({
  value,
  label,
  color = 'primary',
  showValue = false,
  className = '',
}: ProgressBarProps) {
  const percentage = value !== undefined ? Math.min(100, Math.max(0, value)) : 0;

  return (
    <div className={cn('w-full', className)}>
      {(label || showValue) && (
        <div className="mb-1.5 flex justify-between">
          {label && (
            <span className="text-sm font-medium text-foreground">{label}</span>
          )}
          {showValue && value !== undefined && (
            <span className="text-sm font-medium text-muted-foreground">
              {Math.round(percentage)}%
            </span>
          )}
        </div>
      )}
      <Progress
        value={value !== undefined ? percentage : undefined}
        className={cn(indicatorColors[color])}
      />
    </div>
  );
}
