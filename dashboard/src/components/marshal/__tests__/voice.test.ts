// Marshal's voice (spec 4.6) as a lint over the desk's own source: no chatbot
// phrases, no emoji or sparkle, no injected HTML. The model's words are
// checked on the server (validate.py); these are the words the dashboard owns.

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { DESK_COPY, SERVER_COPY, dailyLimitText } from '../../../lib/marshalDesk';

const ROOT = new URL('../../../', import.meta.url);

/** Every source file of the desk: components/marshal (not its tests), its library, hook and mark. */
function deskSources(): { file: string; text: string }[] {
  const dir = new URL('components/marshal/', ROOT);
  const files = readdirSync(dir)
    .filter((f) => /\.tsx?$/.test(f) && statSync(new URL(f, dir)).isFile())
    .map((f) => `components/marshal/${f}`);
  files.push('lib/marshalDesk.ts', 'hooks/marshalDesk.ts', 'brand/TrailMark.tsx');
  return files.map((file) => ({ file, text: readFileSync(new URL(file, ROOT), 'utf8') }));
}

const BANNED = ['How can I help', "I'm here to help", 'Great question', 'Sure!', 'AI Assistant', "I'm sorry", 'Sorry,', '✨'];
const EMOJI = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}]/u;

describe('voice lint', () => {
  const sources = deskSources();

  it('reads the whole desk', () => {
    expect(sources.length).toBeGreaterThanOrEqual(20);
  });

  it('no banned phrase, no emoji, no Sparkles, no dangerouslySetInnerHTML', () => {
    for (const { file, text } of sources) {
      for (const phrase of BANNED) expect(text.includes(phrase), `${file}: ${phrase}`).toBe(false);
      expect(EMOJI.test(text), `${file}: emoji`).toBe(false);
      expect(text, file).not.toMatch(/Sparkles/);
      expect(text, file).not.toContain('dangerouslySetInnerHTML');
    }
  });

  it('every sentence the desk says: no I, no exclamation mark, under 20 words', () => {
    const lines = [...Object.values(DESK_COPY), ...Object.values(SERVER_COPY), dailyLimitText(40)];
    for (const line of lines) {
      expect(line, line).not.toMatch(/\b(I|I'm|I've|I'll|I'd)\b/);
      expect(line, line).not.toContain('!');
      for (const sentence of line.split(/(?<=[.?:;])\s+/)) {
        expect(sentence.split(/\s+/).filter(Boolean).length, sentence).toBeLessThan(20);
      }
    }
  });

  it('the palette no longer uses the sparkle icon', () => {
    const palette = readFileSync(new URL('components/CommandPalette.tsx', ROOT), 'utf8');
    expect(palette).not.toMatch(/\bSparkles\b/);
    expect(palette).toContain("import { TrailMark } from '../brand/TrailMark';");
  });
});
