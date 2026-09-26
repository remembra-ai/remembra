import { useState, useEffect, useRef, useCallback } from 'react';
import type { ElementType } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Search,
  Database,
  Users,
  Orbit,
  Plug,
  History,
  BarChart3,
  TrendingDown,
  Bug,
  Key,
  CreditCard,
  Settings,
  Plus,
  FolderOpen,
  UsersRound,
  Shield,
  ArrowRight,
  Loader2,
  Sparkles,
  Command,
  House,
  GitCommitVertical,
  Bot,
  Inbox,
  PenLine,
  Keyboard,
  Brain,
  HardHat,
  Navigation,
  UserSearch,
  Snowflake,
  Pause,
  Save,
  MessageSquare,
  KeyRound,
  Copy,
  CornerDownLeft,
} from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { api } from '../lib/api';
import type { Memory } from '../lib/api';
import { navigate, type TabType } from '../lib/nav';
import { useCrewSocket } from '../hooks/useCrewSocket';
import { crewApi } from '../lib/crew/api';
import {
  CREW_COMMANDS,
  answer as answerCrewStep,
  back as backCrewStep,
  flowErrorMessage,
  nextStep as nextCrewStep,
  runFlow,
  startFlow,
  validateText,
  type CrewCommandId,
  type CrewFlow,
  type FlowContext,
  type FlowCrew,
  type PickOption,
} from '../lib/crew/commands';
import { useCrewList } from '../lib/crew/hooks';
import { useCrewRoute } from '../lib/crew/routes';

const CREW_ICONS: Record<CrewCommandId, ElementType> = {
  go: Navigation,
  'who-holds': UserSearch,
  freeze: Snowflake,
  pause: Pause,
  checkpoint: Save,
  post: MessageSquare,
  bypass: KeyRound,
};

interface CommandPaletteProps {
  isOpen: boolean;
  onClose: () => void;
  onNavigate: (tab: TabType) => void;
  onNewMemory: () => void;
  onShowShortcuts?: () => void;
  isAdmin?: boolean;
}

interface CommandItem {
  id: string;
  label: string;
  description?: string;
  icon: ElementType;
  section: string;
  action: () => void;
  keywords?: string[];
  shortcut?: string;
}

const overlayVariants = {
  hidden: { opacity: 0 },
  visible: { opacity: 1 },
};

const panelVariants = {
  hidden: {
    opacity: 0,
    scale: 0.98,
    y: -8,
  },
  visible: {
    opacity: 1,
    scale: 1,
    y: 0,
    transition: {
      duration: 0.18,
      ease: [0.22, 1, 0.36, 1] as [number, number, number, number],
    },
  },
  exit: {
    opacity: 0,
    scale: 0.98,
    y: -4,
    transition: { duration: 0.12 },
  },
};

export function CommandPalette({ isOpen, onClose, onNavigate, onNewMemory, onShowShortcuts, isAdmin = false }: CommandPaletteProps) {
  const [query, setQuery] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [mode, setMode] = useState<'commands' | 'search' | 'crew'>('commands');
  const [searchResults, setSearchResults] = useState<Memory[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Crew commands (§9.1): a short flow per command (crew → target → reason → run).
  const crewList = useCrewList(isOpen);
  const crewRoute = useCrewRoute();
  const [flow, setFlow] = useState<CrewFlow | null>(null);
  const [flowPinned, setFlowPinned] = useState(false);
  const [flowRunning, setFlowRunning] = useState(false);
  const [flowError, setFlowError] = useState<string | null>(null);
  const [flowResult, setFlowResult] = useState<{ message: string; copy: string } | null>(null);
  const flowCrew = useCrewSocket(isOpen && flow?.crew ? flow.crew.crewId : null);
  const flowCtx: FlowContext = {
    crews: crewList.items,
    stateOf: (crewId) => (crewId === flowCrew.crewId ? flowCrew.state : null),
  };
  const crewStep = flow && !flowResult ? nextCrewStep(flow, flowCtx) : null;
  const crewOptions: PickOption[] =
    crewStep?.kind === 'pick'
      ? crewStep.options.filter((o) => {
          const q = query.trim().toLowerCase();
          return !q || o.label.toLowerCase().includes(q) || !!o.description?.toLowerCase().includes(q);
        })
      : [];
  const currentCrew: FlowCrew | null = (() => {
    if (!crewRoute?.project) return null;
    const item = crewList.items.find((c) => c.crew.project_id === crewRoute.project);
    return item ? { crewId: item.crew.id, project: item.crew.project_id } : null;
  })();
  const crewReady = crewList.status === 'ready' || crewList.items.length > 0;
  const humanLogin = api.getAuthMode() === 'jwt';

  const resetFlow = () => {
    setFlow(null);
    setFlowPinned(false);
    setFlowRunning(false);
    setFlowError(null);
    setFlowResult(null);
  };

  const executeFlow = async (ready: CrewFlow) => {
    setFlowRunning(true);
    setFlowError(null);
    try {
      const result = await runFlow(ready, flowCtx, crewApi);
      if (result.copy) {
        setFlowResult({ message: result.message, copy: result.copy });
        return;
      }
      toast.success(result.message);
      if (result.href) window.location.hash = result.href;
      resetFlow();
      setMode('commands');
      onClose();
    } catch (err) {
      setFlowError(flowErrorMessage(err));
    } finally {
      setFlowRunning(false);
    }
  };

  const advanceFlow = (next: CrewFlow) => {
    setFlow(next);
    setQuery('');
    setSelectedIndex(0);
    setFlowError(null);
    if (nextCrewStep(next, flowCtx).kind === 'ready') void executeFlow(next);
  };

  const startCrewCommand = (command: CrewCommandId) => {
    resetFlow();
    setMode('crew');
    setFlowPinned(currentCrew !== null);
    advanceFlow(startFlow(command, currentCrew));
  };

  const chooseCrewOption = (value: string) => {
    if (!flow || flowRunning) return;
    const next = answerCrewStep(flow, flowCtx, value);
    if (next !== flow) advanceFlow(next);
  };

  const submitCrewText = () => {
    if (!flow || flowRunning || crewStep?.kind !== 'text') return;
    const problem = validateText(crewStep, query);
    if (problem) {
      setFlowError(problem);
      return;
    }
    advanceFlow(answerCrewStep(flow, flowCtx, query));
  };

  const crewStepBack = () => {
    if (flowResult) {
      resetFlow();
      setMode('commands');
      onClose();
      return;
    }
    const prev = flow ? backCrewStep(flow, flowPinned) : null;
    if (!prev) {
      resetFlow();
      setMode('commands');
      setQuery('');
      return;
    }
    setFlow(prev);
    setQuery('');
    setSelectedIndex(0);
    setFlowError(null);
  };

  const go = (tab: TabType) => () => {
    onNavigate(tab);
    onClose();
  };
  const commands: CommandItem[] = [
    // Quick Actions
    { id: 'write-agent', label: 'Write to an agent', description: 'Leads its next session brief', icon: PenLine, section: 'Actions', action: () => { navigate('inbox', { compose: '1' }); onClose(); }, keywords: ['message', 'inbox', 'send', 'note'], shortcut: 'c' },
    { id: 'search-memories', label: 'Search memories', description: 'Semantic search across all memories', icon: Search, section: 'Actions', action: () => { setMode('search'); setQuery(''); }, keywords: ['find', 'recall', 'query'], shortcut: '/' },
    { id: 'new-memory', label: 'Store a memory', description: 'Create a new memory entry', icon: Plus, section: 'Actions', action: () => { onNewMemory(); onClose(); }, keywords: ['add', 'create', 'store'] },
    ...(onShowShortcuts ? [{ id: 'shortcuts', label: 'Keyboard shortcuts', icon: Keyboard, section: 'Actions', action: () => onShowShortcuts(), keywords: ['keys', 'help'], shortcut: '?' }] : []),
    // Relay
    { id: 'nav-home', label: 'Home', description: 'Mission control', icon: House, section: 'Relay', action: go('home'), keywords: ['mission', 'dashboard', 'overview'], shortcut: 'g h' },
    { id: 'nav-crews', label: 'Crews', description: 'Every project with agents on it', icon: HardHat, section: 'Relay', action: go('crews'), keywords: ['crew', 'site', 'board', 'projects', 'live'] },
    { id: 'nav-trail', label: 'Trail', description: 'Every handoff, newest first', icon: GitCommitVertical, section: 'Relay', action: go('trail'), keywords: ['handoff', 'log', 'history', 'sessions'], shortcut: 'g t' },
    { id: 'nav-agents', label: 'Agents', description: 'Activity per agent', icon: Bot, section: 'Relay', action: go('agents'), keywords: ['claude', 'codex', 'cursor', 'gemini'], shortcut: 'g a' },
    { id: 'nav-inbox', label: 'Inbox', description: 'Messages between agents', icon: Inbox, section: 'Relay', action: go('inbox'), keywords: ['messages', 'notes'], shortcut: 'g i' },
    // Memory + graph
    { id: 'nav-memories', label: 'Memories', description: 'Browse stored memories', icon: Database, section: 'Memory', action: go('memories'), keywords: ['list', 'browse'], shortcut: 'g m' },
    { id: 'nav-timeline', label: 'Timeline', description: 'Memory creation over time', icon: History, section: 'Memory', action: go('timeline') },
    { id: 'nav-analytics', label: 'Analytics', description: 'Usage metrics and trends', icon: BarChart3, section: 'Memory', action: go('analytics'), keywords: ['usage', 'metrics', 'stats'] },
    { id: 'nav-decay', label: 'Decay report', description: 'Memory retention analysis', icon: TrendingDown, section: 'Memory', action: go('decay') },
    { id: 'nav-debugger', label: 'Query debugger', description: 'Inspect recall quality', icon: Bug, section: 'Memory', action: go('debugger') },
    { id: 'nav-graph', label: 'Constellation', description: 'Live graph of agents, projects and entities', icon: Orbit, section: 'Graph', action: go('graph'), keywords: ['graph', 'network', 'knowledge', 'map'], shortcut: 'g g' },
    { id: 'nav-entities', label: 'Entities', description: 'People, concepts, and products', icon: Users, section: 'Graph', action: go('entities'), keywords: ['people', 'concepts'] },
    { id: 'nav-brain', label: 'Brain', description: 'Themes and surprising links', icon: Brain, section: 'Graph', action: go('brain'), keywords: ['insights', 'communities'] },
    // Settings
    { id: 'nav-settings', label: 'Settings', description: 'Profile and preferences', icon: Settings, section: 'Settings', action: go('settings'), shortcut: 'g s' },
    { id: 'nav-keys', label: 'API keys', description: 'Keys for agents and apps', icon: Key, section: 'Settings', action: go('keys'), keywords: ['token', 'access'] },
    { id: 'nav-connections', label: 'Apps & connections', description: 'Claude, ChatGPT and other connected apps', icon: Plug, section: 'Settings', action: go('connections'), keywords: ['connector', 'oauth', 'revoke', 'claude', 'chatgpt'] },
    { id: 'nav-billing', label: 'Billing', description: 'Plan and usage', icon: CreditCard, section: 'Settings', action: go('billing'), keywords: ['plan', 'credits', 'upgrade'] },
    { id: 'nav-teams', label: 'Teams', description: 'Collaboration', icon: UsersRound, section: 'Settings', action: go('teams') },
    { id: 'nav-projects', label: 'Projects', description: 'Memory workspaces', icon: FolderOpen, section: 'Settings', action: go('projects') },
    ...(isAdmin ? [{ id: 'nav-admin', label: 'Admin', description: 'Operate the service', icon: Shield, section: 'Settings', action: go('admin') }] : []),
    // Crew (only once the server answered GET /crews; human-only actions need a dashboard login)
    ...(crewReady
      ? CREW_COMMANDS.filter((c) => humanLogin || !c.humanOnly).map((c) => ({
          id: `crew-${c.id}`,
          label: c.label,
          description: c.description,
          icon: CREW_ICONS[c.id],
          section: 'Crew',
          action: () => startCrewCommand(c.id),
          keywords: c.keywords,
        }))
      : []),
  ];

  // Filter commands based on query
  const filteredCommands = query.trim()
    ? commands.filter(cmd => {
        const q = query.toLowerCase();
        return (
          cmd.label.toLowerCase().includes(q) ||
          cmd.description?.toLowerCase().includes(q) ||
          cmd.keywords?.some(k => k.includes(q))
        );
      })
    : commands;

  // Group by section
  const sections = filteredCommands.reduce<Record<string, CommandItem[]>>((acc, cmd) => {
    if (!acc[cmd.section]) acc[cmd.section] = [];
    acc[cmd.section].push(cmd);
    return acc;
  }, {});

  const flatItems = filteredCommands;

  // Semantic search
  const performSearch = useCallback(async (q: string) => {
    if (!q.trim()) {
      setSearchResults([]);
      return;
    }
    setSearchLoading(true);
    try {
      const results = await api.recallMemories({ query: q, limit: 8 });
      setSearchResults(results.memories || []);
    } catch {
      setSearchResults([]);
    } finally {
      setSearchLoading(false);
    }
  }, []);

  // Reset state when opening
  useEffect(() => {
    if (isOpen) {
      setQuery('');
      setSelectedIndex(0);
      setMode('commands');
      setSearchResults([]);
      setFlow(null);
      setFlowPinned(false);
      setFlowRunning(false);
      setFlowError(null);
      setFlowResult(null);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [isOpen]);

  // Keyboard navigation
  useEffect(() => {
    if (!isOpen) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (mode === 'crew') {
        if (e.key === 'Escape') {
          e.preventDefault();
          crewStepBack();
        } else if (e.key === 'Backspace' && query === '' && !flowRunning) {
          e.preventDefault();
          crewStepBack();
        } else if (e.key === 'ArrowDown' && crewStep?.kind === 'pick') {
          e.preventDefault();
          setSelectedIndex((i) => Math.min(i + 1, Math.max(0, crewOptions.length - 1)));
        } else if (e.key === 'ArrowUp' && crewStep?.kind === 'pick') {
          e.preventDefault();
          setSelectedIndex((i) => Math.max(i - 1, 0));
        } else if (e.key === 'Enter') {
          e.preventDefault();
          if (crewStep?.kind === 'pick' && crewOptions[selectedIndex]) chooseCrewOption(crewOptions[selectedIndex].value);
          else if (crewStep?.kind === 'text') submitCrewText();
        }
        return;
      }
      if (e.key === 'Escape') {
        if (mode === 'search') {
          setMode('commands');
          setQuery('');
          setSearchResults([]);
        } else {
          onClose();
        }
        return;
      }

      const maxIndex = mode === 'search' ? searchResults.length - 1 : flatItems.length - 1;

      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSelectedIndex(i => Math.min(i + 1, maxIndex));
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSelectedIndex(i => Math.max(i - 1, 0));
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (mode === 'commands' && flatItems[selectedIndex]) {
          flatItems[selectedIndex].action();
        }
        // For search mode, could open memory detail
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  });

  // Auto-search with debounce in search mode
  useEffect(() => {
    if (mode !== 'search') return;
    const timer = setTimeout(() => performSearch(query), 350);
    return () => clearTimeout(timer);
  }, [query, mode, performSearch]);

  // Scroll selected item into view
  useEffect(() => {
    if (!listRef.current) return;
    const selected = listRef.current.querySelector('[data-selected="true"]');
    selected?.scrollIntoView({ block: 'nearest' });
  }, [selectedIndex]);

  return (
    <AnimatePresence>
      {isOpen && (
        <motion.div
          className="fixed inset-0 z-[100] flex items-start justify-center pt-[15vh] px-4 modal-backdrop"
          variants={overlayVariants}
          initial="hidden"
          animate="visible"
          exit="hidden"
          transition={{ duration: 0.15 }}
          onClick={onClose}
          role="dialog"
          aria-modal="true"
          aria-label="Search and commands"
        >
          <motion.div
            className="w-full max-w-[560px] modal-surface rounded-[3px] overflow-hidden"
            variants={panelVariants}
            initial="hidden"
            animate="visible"
            exit="exit"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Search Input */}
            <div className="flex items-center gap-3 px-4 py-3.5 border-b border-[hsl(var(--border)/0.4)]">
              {mode === 'search' ? (
                <Sparkles className="w-4 h-4 text-[hsl(var(--primary))] flex-shrink-0" />
              ) : mode === 'crew' ? (
                <HardHat className="w-4 h-4 text-signal-ink flex-shrink-0" />
              ) : (
                <Search className="w-4 h-4 text-[hsl(var(--muted-foreground))] flex-shrink-0" />
              )}
              <input
                ref={inputRef}
                type="text"
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setSelectedIndex(0);
                  if (mode === 'crew') setFlowError(null);
                }}
                placeholder={
                  mode === 'search'
                    ? 'Search memories semantically...'
                    : mode === 'crew'
                      ? crewStep?.kind === 'text'
                        ? crewStep.placeholder
                        : crewStep?.kind === 'pick'
                          ? `${crewStep.title} (type to filter)`
                          : ''
                      : 'Type a command or search...'
                }
                maxLength={crewStep?.kind === 'text' ? crewStep.maxLength : undefined}
                disabled={mode === 'crew' && (flowRunning || !!flowResult)}
                className="cmdk-input"
                aria-label={mode === 'search' ? 'Search memories' : mode === 'crew' ? crewStep && crewStep.kind !== 'ready' ? crewStep.title : 'Crew command' : 'Type a command'}
                autoComplete="off"
                spellCheck={false}
              />
              {mode === 'crew' && (
                <button
                  onClick={crewStepBack}
                  className="text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] px-2 py-1 rounded-md bg-[hsl(var(--muted)/0.5)] flex-shrink-0"
                >
                  ESC
                </button>
              )}
              {mode === 'search' && (
                <button
                  onClick={() => { setMode('commands'); setQuery(''); setSearchResults([]); }}
                  className="text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] px-2 py-1 rounded-md bg-[hsl(var(--muted)/0.5)] flex-shrink-0"
                >
                  ESC
                </button>
              )}
              {mode === 'commands' && (
                <kbd className="flex items-center gap-0.5 text-[10px] text-[hsl(var(--muted-foreground))] px-1.5 py-0.5 rounded-md bg-[hsl(var(--muted)/0.5)] border border-[hsl(var(--border)/0.5)] flex-shrink-0">
                  <Command className="w-2.5 h-2.5" />K
                </kbd>
              )}
            </div>

            {/* Results */}
            <div ref={listRef} className="max-h-[360px] overflow-y-auto py-2 px-2">
              {mode === 'crew' ? (
                <CrewFlowBody
                  flow={flow}
                  step={crewStep}
                  options={crewOptions}
                  selectedIndex={selectedIndex}
                  onHover={setSelectedIndex}
                  onChoose={chooseCrewOption}
                  onSubmitText={submitCrewText}
                  running={flowRunning}
                  error={flowError ?? (flowCrew.status === 'not_found' ? 'That crew is no longer visible to you.' : null)}
                  result={flowResult}
                  onDone={() => {
                    resetFlow();
                    onClose();
                  }}
                />
              ) : mode === 'commands' ? (
                <>
                  {Object.entries(sections).map(([section, items]) => (
                    <div key={section} className="mb-1">
                      <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-[0.24em] text-[hsl(var(--muted-foreground))]">
                        {section}
                      </div>
                      {items.map((item) => {
                        const Icon = item.icon;
                        const globalIndex = flatItems.indexOf(item);
                        const isSelected = globalIndex === selectedIndex;
                        return (
                          <button
                            key={item.id}
                            data-selected={isSelected}
                            onClick={() => item.action()}
                            onMouseEnter={() => setSelectedIndex(globalIndex)}
                            className={clsx('cmdk-item w-full text-left', isSelected && 'bg-[hsl(var(--primary)/0.1)] text-[hsl(var(--foreground))]')}
                          >
                            <Icon className="w-4 h-4 flex-shrink-0" />
                            <div className="flex-1 min-w-0">
                              <span className="text-sm">{item.label}</span>
                              {item.description && (
                                <span className="ml-2 hidden text-xs text-[hsl(var(--muted-foreground))] sm:inline">
                                  {item.description}
                                </span>
                              )}
                            </div>
                            {item.shortcut && (
                              <kbd className="flex-shrink-0 rounded-[2px] border border-rule px-1.5 font-mono text-[10px] text-ink-3">{item.shortcut}</kbd>
                            )}
                            {isSelected && <ArrowRight className="w-3.5 h-3.5 flex-shrink-0 text-signal-ink" />}
                          </button>
                        );
                      })}
                    </div>
                  ))}
                  {flatItems.length === 0 && (
                    <div className="py-8 text-center text-sm text-[hsl(var(--muted-foreground))]">
                      No commands found for "{query}"
                    </div>
                  )}
                </>
              ) : (
                <>
                  {searchLoading && (
                    <div className="flex items-center justify-center gap-2 py-8 text-sm text-[hsl(var(--muted-foreground))]">
                      <Loader2 className="w-4 h-4 animate-spin" />
                      Searching memories...
                    </div>
                  )}
                  {!searchLoading && searchResults.length === 0 && query.trim() && (
                    <div className="py-8 text-center text-sm text-[hsl(var(--muted-foreground))]">
                      No memories found for "{query}"
                    </div>
                  )}
                  {!searchLoading && searchResults.length === 0 && !query.trim() && (
                    <div className="py-8 text-center text-sm text-[hsl(var(--muted-foreground))]">
                      Type to search memories semantically
                    </div>
                  )}
                  {searchResults.map((memory, index) => (
                    <button
                      key={memory.id}
                      data-selected={index === selectedIndex}
                      onMouseEnter={() => setSelectedIndex(index)}
                      className={clsx(
                        'cmdk-item w-full text-left',
                        index === selectedIndex && 'bg-[hsl(var(--primary)/0.1)] text-[hsl(var(--foreground))]'
                      )}
                    >
                      <Database className="w-4 h-4 flex-shrink-0 text-[hsl(var(--primary))]" />
                      <div className="flex-1 min-w-0">
                        <p className="text-sm truncate">{memory.content}</p>
                        {memory.relevance !== undefined && (
                          <span className="text-[11px] text-[hsl(var(--muted-foreground))]">
                            {(memory.relevance * 100).toFixed(0)}% match
                          </span>
                        )}
                      </div>
                    </button>
                  ))}
                </>
              )}
            </div>

            {/* Footer */}
            <div className="flex items-center gap-4 px-4 py-2.5 border-t border-[hsl(var(--border)/0.4)] text-[10px] text-[hsl(var(--muted-foreground))]">
              <span className="flex items-center gap-1">
                <kbd className="px-1 py-0.5 rounded bg-[hsl(var(--muted)/0.5)] border border-[hsl(var(--border)/0.5)]">↑↓</kbd>
                navigate
              </span>
              <span className="flex items-center gap-1">
                <kbd className="px-1 py-0.5 rounded bg-[hsl(var(--muted)/0.5)] border border-[hsl(var(--border)/0.5)]">↵</kbd>
                select
              </span>
              <span className="flex items-center gap-1">
                <kbd className="px-1.5 py-0.5 rounded bg-[hsl(var(--muted)/0.5)] border border-[hsl(var(--border)/0.5)]">esc</kbd>
                close
              </span>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/** The current step of a crew command flow: pick list, text entry, progress, error or the one-time result. */
function CrewFlowBody({
  flow,
  step,
  options,
  selectedIndex,
  onHover,
  onChoose,
  onSubmitText,
  running,
  error,
  result,
  onDone,
}: {
  flow: CrewFlow | null;
  step: ReturnType<typeof nextCrewStep> | null;
  options: PickOption[];
  selectedIndex: number;
  onHover: (index: number) => void;
  onChoose: (value: string) => void;
  onSubmitText: () => void;
  running: boolean;
  error: string | null;
  result: { message: string; copy: string } | null;
  onDone: () => void;
}) {
  const muted = 'text-[hsl(var(--muted-foreground))]';
  const crumb = flow?.crew ? (
    <div className={clsx('px-3 pb-1.5 pt-1 font-mono text-[10px] uppercase tracking-[0.2em]', muted)}>{flow.crew.project}</div>
  ) : null;
  const errorLine = error ? (
    <p role="alert" className="mx-3 my-2 border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
      {error}
    </p>
  ) : null;

  if (result) {
    return (
      <div className="px-3 py-3">
        <p className="text-sm text-ink">{result.message}</p>
        <div className="mt-3 flex items-center gap-2">
          <code className="flex-1 select-all rounded-[2px] border border-rule px-2 py-1.5 font-mono text-sm text-ink">{result.copy}</code>
          <button
            type="button"
            className="rr-btn-ghost inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs"
            onClick={() => {
              void navigator.clipboard?.writeText(result.copy).then(
                () => toast.success('Copied'),
                () => toast.error('Could not copy; select the code instead'),
              );
            }}
          >
            <Copy className="h-3.5 w-3.5" aria-hidden="true" /> Copy
          </button>
        </div>
        <p className={clsx('mt-2 text-xs', muted)}>Shown once. Give it to the person at that terminal; it works for one use.</p>
        <button type="button" className="rr-btn-ghost mt-3 px-2.5 py-1.5 text-xs" onClick={onDone}>
          Done
        </button>
      </div>
    );
  }
  if (running) {
    return (
      <div className={clsx('flex items-center justify-center gap-2 py-8 text-sm', muted)}>
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> Working…
      </div>
    );
  }
  if (!step || step.kind === 'ready') {
    return (
      <>
        {crumb}
        {errorLine}
      </>
    );
  }
  if (step.kind === 'loading') {
    return (
      <>
        {crumb}
        <div className={clsx('flex items-center justify-center gap-2 py-8 text-sm', muted)}>
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> {step.title}
        </div>
        {errorLine}
      </>
    );
  }
  if (step.kind === 'text') {
    return (
      <>
        {crumb}
        <div className="px-3 py-2">
          <p className="text-sm text-ink">{step.title}</p>
          <p className={clsx('mt-1 text-xs', muted)}>Type it above, then press Enter. Backspace on an empty field goes back.</p>
          <button type="button" className="rr-btn-ghost mt-3 inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs" onClick={onSubmitText}>
            <CornerDownLeft className="h-3.5 w-3.5" aria-hidden="true" /> Submit
          </button>
        </div>
        {errorLine}
      </>
    );
  }
  return (
    <>
      {crumb}
      <div className={clsx('px-3 py-1.5 text-[10px] font-semibold uppercase tracking-[0.24em]', muted)}>{step.title}</div>
      {options.map((option, index) => {
        const isSelected = index === selectedIndex;
        return (
          <button
            key={option.value}
            type="button"
            data-selected={isSelected}
            aria-disabled={option.disabled ? true : undefined}
            onClick={() => onChoose(option.value)}
            onMouseEnter={() => onHover(index)}
            className={clsx(
              'cmdk-item w-full text-left',
              isSelected && 'bg-[hsl(var(--primary)/0.1)] text-[hsl(var(--foreground))]',
              option.disabled && 'opacity-50',
            )}
          >
            <div className="min-w-0 flex-1">
              <span className="font-mono text-sm">{option.label}</span>
              {(option.disabled || option.description) && (
                <span className={clsx('ml-2 text-xs', muted)}>{option.disabled ?? option.description}</span>
              )}
            </div>
            {isSelected && !option.disabled && <ArrowRight className="h-3.5 w-3.5 flex-shrink-0 text-signal-ink" />}
          </button>
        );
      })}
      {options.length === 0 && <div className={clsx('py-8 text-center text-sm', muted)}>{step.options.length ? 'No match.' : step.empty}</div>}
      {errorLine}
    </>
  );
}
