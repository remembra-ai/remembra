// Pixel sprites for the Constellation: each node kind has its own shape so
// agents, projects, trail entries and entities read apart at a glance.
// '#' body, '+' lit pixel (top-left light), 'o' signal accent, '.' empty.

import type { NodeKind } from './model';

export const SPRITES: Record<string, string[]> = {
  // Agents: a round chip in the agent's lane colour, lit from the top left.
  agent: ['..###..', '.#+###.', '#+#####', '#######', '#######', '.#####.', '..###..'],
  // Projects: a thick box with cut corners (the place work lands).
  project: ['.#######.', '#########', '##.....##', '##.....##', '##..o..##', '##.....##', '##.....##', '#########', '.#######.'],
  project_quiet: ['.#######.', '#########', '##.....##', '##.....##', '##.....##', '##.....##', '##.....##', '#########', '.#######.'],
  // Handoffs: the flat baton capsule. Checkpoints: a hollow square.
  handoff: ['.####.', '######', '.####.'],
  checkpoint: ['###', '#.#', '###'],
  // Entities: diamonds, larger for entities in more memories.
  entity_1: ['.#.', '###', '.#.'],
  entity_2: ['..#..', '.###.', '#####', '.###.', '..#..'],
  entity_3: ['...#...', '..###..', '.##+##.', '#######', '.#####.', '..###..', '...#...'],
};

export function spriteKey(kind: NodeKind, weight: number, active: boolean): string {
  if (kind === 'entity') return `entity_${Math.min(3, Math.max(1, weight))}`;
  if (kind === 'project') return active ? 'project' : 'project_quiet';
  return kind;
}

export function spriteSize(key: string): { w: number; h: number } {
  const rows = SPRITES[key];
  return { w: rows[0].length, h: rows.length };
}

/** Rasterise a sprite at `unit` device pixels per sprite pixel. */
export function renderSprite(key: string, unit: number, body: string, lit: string, accent: string): HTMLCanvasElement {
  const rows = SPRITES[key];
  const canvas = document.createElement('canvas');
  canvas.width = rows[0].length * unit;
  canvas.height = rows.length * unit;
  const ctx = canvas.getContext('2d');
  if (!ctx) return canvas;
  rows.forEach((row, j) => {
    for (let i = 0; i < row.length; i += 1) {
      const ch = row[i];
      if (ch === '.') continue;
      ctx.fillStyle = ch === '+' ? lit : ch === 'o' ? accent : body;
      ctx.fillRect(i * unit, j * unit, unit, unit);
    }
  });
  return canvas;
}
