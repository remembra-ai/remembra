import { DESK_COPY, type CommandKind, type DeskCommand } from '../../lib/marshalDesk';
import { CopyCommand } from '../relay/ui';

const GROUP_LABEL: Record<CommandKind, string> = {
  terminal: 'A command to run in your terminal',
  codex_ui: 'A command to type in the Codex CLI',
  agent: 'A line to give your agent',
};

/**
 * The call's commands (at most two), under the caption that says who runs
 * them: the user, on their own machine. Marshal has no way to run anything.
 */
export function DeskCommands({ commands }: { commands: DeskCommand[] }) {
  if (commands.length === 0) return null;
  return (
    <div className="min-w-0 space-y-2">
      <p className="font-mono text-[11px] text-ink-3">{DESK_COPY.caption}</p>
      {commands.map((command, index) => (
        <CopyCommand
          key={`${index}:${command.text}`}
          command={command.text}
          label={GROUP_LABEL[command.kind]}
          prompt={command.prompt}
          scroll
          toastText="Copied"
        />
      ))}
    </div>
  );
}
