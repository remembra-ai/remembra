// The dashboard serves every account: no user-visible text names one person. (Agent-facing refusals keep the
// @mani alias, an owner decision; the dashboard never shows it.)

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const SRC = join(__dirname, '..', '..');

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === '__tests__' ? [] : sources(path);
    return /\.(ts|tsx)$/.test(name) ? [path] : [];
  });
}

/** Source without comments (a comment may name the author; the screen may not). */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`])\/\/.*$/gm, '$1');
}

describe('dashboard copy', () => {
  it('never names one owner in text a user sees', () => {
    const hits = sources(SRC).flatMap((path) =>
      code(readFileSync(path, 'utf8'))
        .split('\n')
        .filter((line) => /\bMani\b/.test(line))
        .map((line) => `${path.slice(SRC.length + 1)}: ${line.trim()}`),
    );
    expect(hits).toEqual([]);
  });

  it('says what a decision reads as in the brief', () => {
    const composer = readFileSync(join(SRC, 'components', 'crew', 'channel', 'Composer.tsx'), 'utf8');
    expect(composer).toContain('"confirmed by a human"'); // relay/handoff.py: "Decisions in force (confirmed by a human)"
  });
});
