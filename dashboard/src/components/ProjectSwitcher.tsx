import { useState, useEffect, useRef } from 'react';
import { ChevronDown, Check, FolderOpen, Loader2 } from 'lucide-react';
import clsx from 'clsx';
import { api } from '../lib/api';
import { API_V1 } from '../config';

interface Project {
  id: string;
  namespace: string;
  name: string;
  description?: string;
}

interface ProjectSwitcherProps {
  onProjectChange?: (projectId: string) => void;
}

interface SpaceSummary {
  id: string;
  name: string;
  description?: string;
  project_id: string;
}

export function ProjectSwitcher({ onProjectChange }: ProjectSwitcherProps) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [currentProject, setCurrentProject] = useState<Project | null>(null);
  const [loading, setLoading] = useState(true);
  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const getAuthHeaders = () => {
    const token = api.getJwtToken();
    if (token) {
      return { 'Authorization': `Bearer ${token}` };
    }
    const apiKey = api.getApiKey();
    if (apiKey) {
      return { 'X-API-Key': apiKey };
    }
    return {};
  };

  const emitProjectChange = (projectId: string) => {
    window.dispatchEvent(new CustomEvent('remembra:project-changed', {
      detail: { projectId },
    }));
    onProjectChange?.(projectId);
  };

  // Fetch projects and set current
  useEffect(() => {
    fetchProjects();
  }, []);

  // Close dropdown on outside click
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };

    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside);
      return () => document.removeEventListener('mousedown', handleClickOutside);
    }
  }, [isOpen]);

  const fetchProjects = async () => {
    try {
      setLoading(true);
      const response = await fetch(`${API_V1}/spaces`, {
        headers: getAuthHeaders(),
      });

      if (response.ok) {
        const data = await response.json();
        const projectList: Project[] = (Array.isArray(data) ? data : data.spaces || []).map((space: SpaceSummary) => ({
          id: space.id,
          namespace: space.project_id || space.id,
          name: space.name,
          description: space.description,
        }));
        setProjects(projectList);

        // Determine current project
        const savedProjectId = api.getProjectId();
        let current = projectList.find((p: Project) => p.namespace === savedProjectId);
        
        if (!current && projectList.length > 0) {
          current = projectList[0];
        }

        if (current) {
          setCurrentProject(current);
          api.setProjectId(current.namespace);
          emitProjectChange(current.namespace);
        } else {
          // Fallback: create a default project representation
          const defaultProject = {
            id: savedProjectId || 'default',
            namespace: savedProjectId || 'default',
            name: savedProjectId || 'Default Project',
          };
          setCurrentProject(defaultProject);
          api.setProjectId(defaultProject.namespace);
          emitProjectChange(defaultProject.namespace);
        }
      } else {
        // Spaces API not available, show current project ID only
        const projectId = api.getProjectId() || 'default';
        setCurrentProject({
          id: projectId,
          namespace: projectId,
          name: projectId === 'default' ? 'Default Project' : projectId,
        });
        emitProjectChange(projectId);
      }
    } catch (error) {
      console.error('Failed to fetch projects:', error);
      // Fallback
      const projectId = api.getProjectId() || 'default';
      setCurrentProject({
        id: projectId,
        namespace: projectId,
        name: projectId === 'default' ? 'Default Project' : projectId,
      });
      emitProjectChange(projectId);
    } finally {
      setLoading(false);
    }
  };

  const switchProject = (project: Project) => {
    setCurrentProject(project);
    api.setProjectId(project.namespace);
    setIsOpen(false);
    emitProjectChange(project.namespace);
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 px-3 py-2 rounded-xl premium-chip">
        <Loader2 className="w-4 h-4 animate-spin text-[hsl(var(--muted-foreground))]" />
        <span className="text-sm text-[hsl(var(--muted-foreground))]">Loading...</span>
      </div>
    );
  }

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={clsx(
          'flex items-center gap-2 px-3 py-2 rounded-xl premium-chip',
          'hover:bg-[hsl(var(--muted))/0.82] transition-colors',
          'text-sm font-medium',
          isOpen && 'bg-[hsl(var(--muted))/0.82]'
        )}
      >
        <FolderOpen className="w-4 h-4 text-[hsl(var(--shell-glow))]" />
        <span className="text-[hsl(var(--foreground))] max-w-[150px] truncate">
          {currentProject?.name || 'Select Project'}
        </span>
        <ChevronDown className={clsx(
          'w-4 h-4 text-[hsl(var(--muted-foreground))] transition-transform',
          isOpen && 'rotate-180'
        )} />
      </button>

      {isOpen && projects.length > 0 && (
        <div className="absolute top-full left-0 mt-2 w-64 rounded-xl dashboard-surface border border-[hsl(var(--border))/0.72] shadow-2xl z-50 overflow-hidden">
          <div className="p-2 border-b border-[hsl(var(--border))/0.72]">
            <p className="text-[10px] uppercase tracking-[0.24em] text-[hsl(var(--muted-foreground))] px-2 py-1">
              Switch Project
            </p>
          </div>
          <div className="max-h-80 overflow-y-auto p-2 space-y-1">
            {projects.map((project) => (
              <button
                key={project.id}
                onClick={() => switchProject(project)}
                className={clsx(
                  'w-full flex items-center justify-between gap-2 px-3 py-2.5 rounded-lg',
                  'text-left transition-colors',
                  currentProject?.id === project.id
                    ? 'bg-[linear-gradient(135deg,hsl(var(--primary))/0.18,hsl(var(--shell-glow))/0.08)] text-[hsl(var(--foreground))]'
                    : 'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))/0.78]'
                )}
              >
                <div className="flex items-center gap-2 min-w-0">
                  <FolderOpen className="w-4 h-4 flex-shrink-0" />
                  <div className="min-w-0">
                    <div className="font-medium truncate text-sm">
                      {project.name}
                    </div>
                    {project.description && (
                      <div className="text-xs text-[hsl(var(--muted-foreground))] truncate">
                        {project.description}
                      </div>
                    )}
                  </div>
                </div>
                {currentProject?.id === project.id && (
                  <Check className="w-4 h-4 text-[hsl(var(--shell-glow))] flex-shrink-0" />
                )}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
