import React from 'react';

export interface FileItem {
  id: string;
  name: string;
  url?: string;
  thumbnailUrl?: string;
  size?: number; // in bytes
  date?: string;
  type?: 'video' | 'image' | 'other';
}

interface FileBrowserProps {
  files: FileItem[];
  onSelect?: (file: FileItem) => void;
  onDownload?: (file: FileItem) => void;
  selectedFileId?: string;
  className?: string;
  emptyMessage?: string;
}

export const FileBrowser: React.FC<FileBrowserProps> = ({
  files,
  onSelect,
  onDownload,
  selectedFileId,
  className = '',
  emptyMessage = 'No files found.',
}) => {
  const formatSize = (bytes?: number) => {
    if (bytes === undefined) return '';
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  if (files.length === 0) {
    return (
      <div className={`flex items-center justify-center p-8 text-gray-500 dark:text-gray-400 border-2 border-dashed border-gray-300 dark:border-gray-700 rounded-lg ${className}`}>
        {emptyMessage}
      </div>
    );
  }

  return (
    <div className={`grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-4 ${className}`}>
      {files.map((file) => (
        <div
          key={file.id}
          className={`group relative border rounded-lg overflow-hidden cursor-pointer transition-all duration-200 hover:shadow-md ${
            selectedFileId === file.id
              ? 'border-blue-500 ring-2 ring-blue-200 dark:ring-blue-900'
              : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'
          }`}
          onClick={() => onSelect && onSelect(file)}
        >
          <div className="aspect-square bg-gray-100 dark:bg-gray-800 flex items-center justify-center overflow-hidden relative">
            {file.thumbnailUrl ? (
              <img
                src={file.thumbnailUrl}
                alt={file.name}
                className="w-full h-full object-cover transition-transform duration-300 group-hover:scale-105"
              />
            ) : (
              <div className="text-gray-400 dark:text-gray-500">
                <svg className="w-12 h-12" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  {file.type === 'video' ? (
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
                  ) : file.type === 'image' ? (
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
                  ) : (
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  )}
                </svg>
              </div>
            )}
            
            {/* Overlay actions */}
            <div className="absolute inset-0 bg-black bg-opacity-0 group-hover:bg-opacity-30 transition-all duration-200 flex items-center justify-center opacity-0 group-hover:opacity-100">
              {onDownload && (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onDownload(file);
                  }}
                  className="p-2 bg-white dark:bg-gray-800 rounded-full shadow-lg hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-200 transition-colors"
                  title="Download"
                >
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                  </svg>
                </button>
              )}
            </div>
          </div>
          
          <div className="p-3 bg-white dark:bg-gray-800">
            <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate" title={file.name}>
              {file.name}
            </p>
            <div className="flex justify-between items-center mt-1">
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {formatSize(file.size)}
              </span>
              {file.date && (
                <span className="text-xs text-gray-400 dark:text-gray-500">
                  {file.date}
                </span>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
};
