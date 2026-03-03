import { Badge } from '@/components/ui/badge';

export type StatusType = 'queued' | 'processing' | 'done' | 'failed' | 'info' | 'warning' | 'active' | string;

interface StatusChipProps {
  status: StatusType;
  className?: string;
}

function getVariant(status: string): 'success' | 'info' | 'warning' | 'error' | 'active' | 'secondary' {
  switch (status.toLowerCase()) {
    case 'done':
    case 'complete':
    case 'success':
      return 'success';
    case 'processing':
    case 'running':
      return 'info';
    case 'queued':
    case 'pending':
      return 'warning';
    case 'failed':
    case 'error':
      return 'error';
    case 'active':
      return 'active';
    default:
      return 'secondary';
  }
}

export function StatusChip({ status, className = '' }: StatusChipProps) {
  return (
    <Badge variant={getVariant(status)} className={className}>
      {status}
    </Badge>
  );
}
