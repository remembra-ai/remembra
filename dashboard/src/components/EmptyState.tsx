import { Database, Users, Share2, BarChart3, Key, Brain, Sparkles, ArrowRight } from 'lucide-react';
import clsx from 'clsx';

interface EmptyStateProps {
  type: 'memories' | 'entities' | 'graph' | 'search' | 'analytics' | 'api-keys' | 'generic';
  searchQuery?: string;
  onAction?: () => void;
  actionLabel?: string;
}

const emptyStates = {
  memories: {
    icon: Database,
    title: 'No memories yet',
    description: 'Start by storing your first memory. Remembra will automatically extract entities and make it searchable.',
    codeSnippet: `from remembra import Memory

memory = Memory()
memory.store("User prefers dark mode")`,
  },
  entities: {
    icon: Users,
    title: 'No entities found',
    description: 'Entities are automatically extracted from memories. Store some memories to see entities appear here.',
    codeSnippet: `memory.store(
  "John from Acme Corp prefers email",
  user_id="user_123"
)`,
  },
  graph: {
    icon: Share2,
    title: 'Entity graph is empty',
    description: 'The knowledge graph visualizes relationships between entities. Add memories with multiple entities to see connections.',
    codeSnippet: null,
  },
  search: {
    icon: Sparkles,
    title: 'No results found',
    description: 'Try adjusting your search terms or filters.',
    codeSnippet: null,
  },
  analytics: {
    icon: BarChart3,
    title: 'No analytics data yet',
    description: 'Analytics will appear once you start using the API. Store and recall memories to see usage patterns.',
    codeSnippet: null,
  },
  'api-keys': {
    icon: Key,
    title: 'No API keys',
    description: 'Create an API key to start integrating Remembra into your applications.',
    codeSnippet: null,
  },
  generic: {
    icon: Brain,
    title: 'Nothing here yet',
    description: 'This section will populate as you use Remembra.',
    codeSnippet: null,
  },
};

export function EmptyState({ type, searchQuery, onAction, actionLabel }: EmptyStateProps) {
  const state = emptyStates[type] || emptyStates.generic;
  const Icon = state.icon;

  const isSearch = type === 'search' && searchQuery;

  return (
    <div className="flex flex-col items-center justify-center py-16 px-4">
      {/* Animated icon container */}
      <div className={clsx(
        'relative mb-6',
        'w-20 h-20 rounded-2xl',
        'bg-gradient-to-br from-signal/20 to-accent/10',
        'border border-signal/20',
        'flex items-center justify-center',
        'shadow-lg shadow-purple-500/10'
      )}>
        {/* Glow effect */}
        <div className="absolute inset-0 rounded-2xl bg-signal/20 blur-xl" />
        
        <Icon className="relative w-10 h-10 text-signal-ink" />
        
        {/* Floating particles */}
        <div className="absolute -top-1 -right-1 w-3 h-3 rounded-full bg-signal/40 animate-pulse" />
        <div className="absolute -bottom-2 -left-2 w-2 h-2 rounded-full bg-signal/40 animate-pulse delay-300" />
      </div>

      {/* Title */}
      <h3 className="text-xl font-semibold text-[hsl(var(--foreground))] mb-2 text-center">
        {isSearch ? `No results for "${searchQuery}"` : state.title}
      </h3>

      {/* Description */}
      <p className="text-[hsl(var(--muted-foreground))] text-center max-w-md mb-6">
        {isSearch 
          ? 'Try different keywords or check your spelling.'
          : state.description
        }
      </p>

      {/* Code snippet */}
      {state.codeSnippet && !isSearch && (
        <div className={clsx(
          'w-full max-w-lg mb-6',
          'rounded-xl overflow-hidden',
          'bg-[#0D0D0D] border border-[hsl(var(--border))]',
          'shadow-lg'
        )}>
          {/* Code header */}
          <div className="flex items-center justify-between px-4 py-2 bg-[hsl(var(--muted))] border-b border-[hsl(var(--border))]">
            <div className="flex items-center gap-2">
              <div className="w-3 h-3 rounded-full bg-red-500/60" />
              <div className="w-3 h-3 rounded-full bg-yellow-500/60" />
              <div className="w-3 h-3 rounded-full bg-green-500/60" />
            </div>
            <span className="text-xs text-[hsl(var(--muted-foreground))]">Python</span>
          </div>
          
          {/* Code content */}
          <pre className="p-4 text-sm overflow-x-auto">
            <code className="text-signal-ink">
              {state.codeSnippet.split('\n').map((line, i) => (
                <div key={i} className="leading-relaxed">
                  {highlightCode(line)}
                </div>
              ))}
            </code>
          </pre>
        </div>
      )}

      {/* Action button */}
      {onAction && actionLabel && (
        <button
          onClick={onAction}
          className={clsx(
            'inline-flex items-center gap-2 px-5 py-2.5 rounded-lg',
            'bg-accent hover:bg-accent-hover',
            'text-white font-medium text-sm',
            'transition-all duration-200',
            'shadow-lg shadow-purple-500/25 hover:shadow-purple-500/40',
            'hover:translate-y-[-1px]'
          )}
        >
          {actionLabel}
          <ArrowRight className="w-4 h-4" />
        </button>
      )}

      {/* Documentation link */}
      {!isSearch && (
        <a
          href="https://docs.remembra.dev"
          target="_blank"
          rel="noopener noreferrer"
          className="mt-4 text-sm text-signal-ink hover:text-signal-ink transition-colors"
        >
          Read the documentation →
        </a>
      )}
    </div>
  );
}

// Tokenize the original text once. React escapes each text token; generated
// markup is never fed back through the keyword/string highlighter.
function highlightCode(line: string): React.ReactNode {
  const tokenPattern = /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|#.*|\b(?:from|import|def|class|return|if|else|for|in|while|try|except)\b|\b\w+(?=\())/g;
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  for (const match of line.matchAll(tokenPattern)) {
    if (match.index > cursor) parts.push(line.slice(cursor, match.index));
    const token = match[0];
    const color = token.startsWith('#') ? 'text-gray-500'
      : token.startsWith('"') || token.startsWith("'") ? 'text-green-400'
      : line[match.index + token.length] === '(' ? 'text-blue-400' : 'text-pink-400';
    parts.push(<span key={match.index} className={color}>{token}</span>);
    cursor = match.index + token.length;
  }
  if (cursor < line.length) parts.push(line.slice(cursor));
  return <span>{parts}</span>;
}
