import { useState } from 'react';
import { type Project } from '../types/api';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';

interface ProjectSelectorProps {
  projects: Project[];
  activeProjectName: string | null;
  onSelect: (project: Project) => void;
  onCreate: (name: string) => Promise<void> | void;
  className?: string;
}

export function ProjectSelector({
  projects,
  activeProjectName,
  onSelect,
  onCreate,
  className = '',
}: ProjectSelectorProps) {
  const [isCreating, setIsCreating] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [open, setOpen] = useState(false);

  const handleCreate = async () => {
    const name = newProjectName.trim();
    if (!name || isSubmitting) return;

    setIsSubmitting(true);
    try {
      await onCreate(name);
      setNewProjectName('');
      setIsCreating(false);
      setOpen(false);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className={className}>
      <DropdownMenu open={open} onOpenChange={(o) => { setOpen(o); if (!o) setIsCreating(false); }}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="w-full border-2 border-border rounded-[var(--border-radius)] bg-card px-3 py-2 text-left shadow-shadow hover:translate-x-[2px] hover:translate-y-[2px] hover:shadow-[2px_2px_0_0_var(--border)] transition-all duration-100"
          >
            <div className="text-[10px] uppercase tracking-wider font-bold text-muted-foreground">Active Project</div>
            <div className="truncate text-sm font-bold text-foreground">
              {activeProjectName ?? 'Select project'}
            </div>
          </button>
        </DropdownMenuTrigger>

        <DropdownMenuContent align="start" className="w-72">
          {!isCreating ? (
            <>
              {projects.length === 0 ? (
                <div className="px-3 py-3 text-sm text-muted-foreground">No projects found</div>
              ) : (
                projects.map((project) => {
                  const isActive = activeProjectName === project.name;
                  return (
                    <DropdownMenuItem
                      key={project.name}
                      className="flex flex-col items-start gap-1 py-2"
                      onSelect={() => onSelect(project)}
                    >
                      <div className="flex w-full items-center justify-between gap-3">
                        <span className="truncate text-sm font-bold">{project.name}</span>
                        {isActive && <Badge variant="active">Active</Badge>}
                      </div>
                      <div className="text-[11px] text-muted-foreground">
                        {project.video_count} videos, {project.caption_count} captions, {project.burned_count} burned
                      </div>
                    </DropdownMenuItem>
                  );
                })
              )}
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onSelect={(e) => {
                  e.preventDefault();
                  setIsCreating(true);
                }}
                className="font-bold text-primary"
              >
                + Create New Project
              </DropdownMenuItem>
            </>
          ) : (
            <div className="p-3" onClick={(e) => e.stopPropagation()}>
              <Input
                placeholder="Project name"
                value={newProjectName}
                onChange={(e) => setNewProjectName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void handleCreate();
                  if (e.key === 'Escape') setIsCreating(false);
                }}
                autoFocus
              />
              <div className="mt-2 flex justify-end gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setIsCreating(false)}
                  disabled={isSubmitting}
                >
                  Cancel
                </Button>
                <Button
                  size="sm"
                  onClick={() => { void handleCreate(); }}
                  disabled={isSubmitting || !newProjectName.trim()}
                >
                  {isSubmitting ? 'Creating...' : 'Create'}
                </Button>
              </div>
            </div>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
