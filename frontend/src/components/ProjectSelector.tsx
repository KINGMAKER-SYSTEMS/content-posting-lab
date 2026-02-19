import { useEffect, useRef, useState } from 'react';
import { type Project } from '../types/api';

interface ProjectSelectorProps {
  projects: Project[];
  activeProject: Project | null;
  onSelect: (project: Project) => void;
  onCreate: (name: string) => Promise<void> | void;
  className?: string;
}

export function ProjectSelector({
  projects,
  activeProject,
  onSelect,
  onCreate,
  className = '',
}: ProjectSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false);
        setIsCreating(false);
      }
    };

    document.addEventListener('mousedown', onClickOutside);
    return () => document.removeEventListener('mousedown', onClickOutside);
  }, []);

  const handleCreate = async () => {
    const name = newProjectName.trim();
    if (!name || isSubmitting) {
      return;
    }

    setIsSubmitting(true);
    try {
      await onCreate(name);
      setNewProjectName('');
      setIsCreating(false);
      setIsOpen(false);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className={`relative ${className}`} ref={dropdownRef}>
      <button
        type="button"
        className="w-full bg-black/30 border border-white/15 rounded-lg px-3 py-2 text-left hover:bg-black/40 focus:outline-none focus:ring-2 focus:ring-purple-500/40"
        onClick={() => setIsOpen((prev) => !prev)}
      >
        <div className="text-[10px] uppercase tracking-wider text-gray-500">Active Project</div>
        <div className="truncate text-sm font-medium text-white">
          {activeProject ? activeProject.name : 'Select project'}
        </div>
      </button>

      {isOpen && (
        <div className="absolute z-50 mt-2 w-full rounded-lg border border-white/15 bg-charcoal shadow-xl">
          {!isCreating ? (
            <>
              <div className="max-h-64 overflow-y-auto py-1">
                {projects.length === 0 ? (
                  <div className="px-3 py-3 text-sm text-gray-500">No projects found</div>
                ) : (
                  projects.map((project) => {
                    const isActive = activeProject?.name === project.name;
                    return (
                      <button
                        key={project.name}
                        type="button"
                        onClick={() => {
                          onSelect(project);
                          setIsOpen(false);
                        }}
                        className={`w-full px-3 py-2 text-left hover:bg-white/5 ${
                          isActive ? 'bg-purple-500/15' : ''
                        }`}
                      >
                        <div className="flex items-center justify-between gap-3">
                          <span className={`truncate text-sm ${isActive ? 'text-purple-200' : 'text-gray-100'}`}>
                            {project.name}
                          </span>
                          {isActive ? (
                            <span className="text-[10px] rounded-full bg-purple-500/20 px-2 py-0.5 text-purple-200">Active</span>
                          ) : null}
                        </div>
                        <div className="mt-1 text-[11px] text-gray-500">
                          {project.video_count} videos, {project.caption_count} captions, {project.burned_count} burned
                        </div>
                      </button>
                    );
                  })
                )}
              </div>

              <button
                type="button"
                className="w-full border-t border-white/10 px-3 py-2 text-left text-sm font-medium text-purple-300 hover:bg-white/5"
                onClick={() => setIsCreating(true)}
              >
                + Create New Project
              </button>
            </>
          ) : (
            <div className="p-3">
              <input
                type="text"
                className="input"
                placeholder="Project name"
                value={newProjectName}
                onChange={(event) => setNewProjectName(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    void handleCreate();
                  }
                  if (event.key === 'Escape') {
                    setIsCreating(false);
                  }
                }}
                autoFocus
              />
              <div className="mt-2 flex justify-end gap-2">
                <button
                  type="button"
                  className="btn btn-secondary py-1.5 text-xs"
                  onClick={() => setIsCreating(false)}
                  disabled={isSubmitting}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn btn-primary py-1.5 text-xs"
                  onClick={() => {
                    void handleCreate();
                  }}
                  disabled={isSubmitting || !newProjectName.trim()}
                >
                  {isSubmitting ? 'Creating...' : 'Create'}
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
