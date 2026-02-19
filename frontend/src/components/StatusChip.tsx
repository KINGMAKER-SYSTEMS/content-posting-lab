import React from 'react';

export type StatusType = 'queued' | 'processing' | 'done' | 'failed' | 'info' | 'warning' | string;

interface StatusChipProps {
  status: StatusType;
  className?: string;
}

export const StatusChip: React.FC<StatusChipProps> = ({ status, className = '' }) => {
  const getStatusColor = (status: string) => {
    switch (status.toLowerCase()) {
      case 'done':
      case 'complete':
      case 'success':
        return 'bg-green-500/20 text-green-300 border border-green-500/20';
      case 'processing':
      case 'running':
        return 'bg-blue-500/20 text-blue-300 border border-blue-500/20';
      case 'queued':
      case 'pending':
        return 'bg-yellow-500/20 text-yellow-300 border border-yellow-500/20';
      case 'failed':
      case 'error':
        return 'bg-red-500/20 text-red-300 border border-red-500/20';
      case 'info':
        return 'bg-gray-500/20 text-gray-300 border border-gray-500/20';
      default:
        return 'bg-gray-500/20 text-gray-300 border border-gray-500/20';
    }
  };

  return (
    <span
      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium capitalize ${getStatusColor(
        status
      )} ${className}`}
    >
      {status}
    </span>
  );
};
