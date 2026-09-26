// remembra.dev/setup.md (the runbook the user's agent follows) gives the
// dashboard's own commands and the slip's own Codex lines. The Python side
// (tests/test_marshal_parity.py) holds the same file to remembra_setup.

import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  PIPX_INSTALL,
  UNINSTALL_STEPS,
  agentConnectCommand,
  doctorCommand,
  oneLineInstall,
  pipxRunDoctorCommand,
  saveKeyCommand,
} from '../agents';
import { CODEX_TRUST_FIX, CODEX_TRUST_LINE } from '../marshal';
import { SHARED_RULES } from '../marshalWords';

const SETUP = readFileSync(new URL('../../../../landing/setup.md', import.meta.url), 'utf8');

/** `{ "4": body, "Taking it off again": body, ... }` */
function sections(): Record<string, string> {
  const out: Record<string, string> = {};
  for (const chunk of SETUP.split('\n## ').slice(1)) {
    const [title, ...rest] = chunk.split('\n');
    const step = /^(\d+)\. /.exec(title);
    out[step ? step[1] : title.trim()] = rest.join('\n');
  }
  return out;
}

function bash(body: string): string[][] {
  return [...body.matchAll(/```bash\n([\s\S]*?)```/g)].map((m) =>
    m[1]
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean),
  );
}

function prose(body: string): string {
  return body.replace(/```[\s\S]*?```/g, '').replace(/`/g, '').split(/\s+/).join(' ');
}

describe('setup.md runs the dashboard catalog', () => {
  const s = sections();

  it('installs, saves the key and writes the hooks with the one-line install, split in its steps', () => {
    const [install, key, apply] = oneLineInstall('').split(' && ');
    expect(install).toBe(PIPX_INSTALL);
    expect(key).toBe(saveKeyCommand('')); // no server named: the machine keeps its own, or Remembra Cloud
    expect(bash(s['4'])).toEqual([[install]]);
    expect(bash(s['5'])).toEqual([[key]]);
    expect(bash(s['7'])[0]).toEqual([apply]);
    expect(bash(s["The same steps in the user's terminal"])).toEqual([[install, key, apply]]);
  });

  it('names an unverified agent the way its checklist row does', () => {
    expect(bash(s['7'])[1]).toEqual([agentConnectCommand('gemini')]);
  });

  it('checks with the doctor lines the why? slip copies', () => {
    expect(bash(s['10'])).toEqual([[doctorCommand()]]);
    expect(prose(s['10'])).toContain(pipxRunDoctorCommand());
  });

  it("tells the user the slip's Codex call and fix, word for word", () => {
    const text = prose(s['8']);
    expect(text).toContain(CODEX_TRUST_LINE);
    expect(text).toContain(CODEX_TRUST_FIX);
    expect(text).toContain(SHARED_RULES.CODEX_TRUST_MISSING.detail);
  });

  it('takes it off with the uninstall steps Settings shows', () => {
    expect(bash(s['Taking it off again'])).toEqual([UNINSTALL_STEPS.map((step) => step.command)]);
  });
});
