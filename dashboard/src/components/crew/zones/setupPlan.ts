// Setup mode (spec §9.5, D37): after the no-zone bootstrap, the temporary zones
// (auto, from folders) become a draft the owner shapes with Keep / Rename /
// Merge / Split / Drop, then saves as `.remembra/zones.yml`.
//
// zones.yml in the repository is the authority for the zones it declares (D9),
// so the dashboard does not write zones: it produces the file (and a patch that
// creates it) for the owner to commit on the default branch. crewd uploads it,
// and it replaces the temporary zones. "Undo" removes the temporary zones
// server-side instead (human-only archive).
//
// Pure functions; the YAML is the subset of the zones.yml grammar the server
// parses in `remembra.crew.policy.parse_zones_yaml` (every string double-quoted).

import { globAnchors, type RepoTreeNode, type ZoneDetail } from './zoneModel';

export const SLUG_RE = /^[a-z0-9][a-z0-9_-]{0,47}$/;
export const TITLE_MAX = 120;
export const ZONES_PATH = '.remembra/zones.yml';
const BUILTIN_SLUG = 'crew-policy';

export interface PlanZone {
  /** Stable key for React and edits (the first source slug, or a derived one). */
  key: string;
  slug: string;
  title: string;
  include: string[];
  /** The temporary zones this entry came from (for the merge/split trail shown on screen). */
  from: string[];
}

export interface SetupPlan {
  zones: PlanZone[];
  /** What happened, newest last (shown as the plan's trail; also what Undo-step pops). */
  log: string[];
}

export function slugify(text: string): string {
  const s = text
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^[-_]+|[-_]+$/g, '');
  return (s || 'zone').slice(0, 48);
}

function uniqueSlug(base: string, taken: ReadonlySet<string>): string {
  let slug = slugify(base);
  if (slug === BUILTIN_SLUG) slug = `${slug}-zone`;
  const stem = slug.slice(0, 44);
  let n = 2;
  while (taken.has(slug)) slug = `${stem}-${n++}`;
  return slug;
}

/** The draft from the temporary (suggested) zones, in slug order. */
export function initialPlan(zones: Iterable<ZoneDetail>): SetupPlan {
  const plan = [...zones]
    .filter((z) => !z.builtin && !z.archived_at && z.source === 'suggested')
    .sort((a, b) => a.slug.localeCompare(b.slug))
    .map((z) => ({ key: z.slug, slug: z.slug, title: z.title || z.slug, include: [...z.include_globs], from: [z.slug] }));
  return { zones: plan, log: [] };
}

/** A draft from `POST /zones/suggest` (a crew with no zones yet). */
export function planFromSuggestions(zones: readonly { slug: string; title?: string | null; include: readonly string[] }[]): SetupPlan {
  return {
    zones: zones.map((z) => ({ key: z.slug, slug: z.slug, title: z.title || z.slug, include: [...z.include], from: [z.slug] })),
    log: [],
  };
}

export function renameZone(plan: SetupPlan, key: string, slug: string, title: string): SetupPlan {
  const zone = plan.zones.find((z) => z.key === key);
  if (!zone) return plan;
  const next = { ...zone, slug: slug.trim(), title: title.trim() };
  return {
    zones: plan.zones.map((z) => (z.key === key ? next : z)),
    log: [...plan.log, `renamed ${zone.slug} to ${next.slug}`],
  };
}

/** Fold `key` into `intoKey`: globs unioned (order kept), the target keeps its name. */
export function mergeZones(plan: SetupPlan, key: string, intoKey: string): SetupPlan {
  if (key === intoKey) return plan;
  const src = plan.zones.find((z) => z.key === key);
  const dst = plan.zones.find((z) => z.key === intoKey);
  if (!src || !dst) return plan;
  const merged: PlanZone = {
    ...dst,
    include: [...new Set([...dst.include, ...src.include])],
    from: [...new Set([...dst.from, ...src.from])],
  };
  return {
    zones: plan.zones.filter((z) => z.key !== key).map((z) => (z.key === intoKey ? merged : z)),
    log: [...plan.log, `merged ${src.slug} into ${dst.slug}`],
  };
}

export function dropZone(plan: SetupPlan, key: string): SetupPlan {
  const zone = plan.zones.find((z) => z.key === key);
  if (!zone) return plan;
  return { zones: plan.zones.filter((z) => z.key !== key), log: [...plan.log, `dropped ${zone.slug}`] };
}

function findFolder(tree: RepoTreeNode | null | undefined, dir: string): RepoTreeNode | null {
  if (!tree) return null;
  let node: RepoTreeNode | undefined = tree;
  for (const part of dir.split('/').filter(Boolean)) {
    node = (node?.children ?? []).find((c) => c.name === part);
    if (!node) return null;
  }
  return node ?? null;
}

/** Why `zone` cannot be split into its subfolders, or null when it can. */
export function splitBlocker(zone: PlanZone, tree: RepoTreeNode | null | undefined): string | null {
  if (zone.include.length !== 1) return 'Only a zone with one folder glob can be split.';
  const [anchor] = globAnchors(zone.include[0]);
  if (!anchor || !anchor.deep || globAnchors(zone.include[0]).length !== 1) return 'Only a folder zone (folder/**) can be split.';
  const folder = findFolder(tree, anchor.dir);
  if (!tree) return 'The repository tree has not been uploaded yet.';
  const kids = (folder?.children ?? []).filter((c) => c.name && !c.name.startsWith('.'));
  if (kids.length < 2) return 'This folder has fewer than two subfolders.';
  return null;
}

/** Replace a folder zone with one zone per subfolder (same position in the list). */
export function splitZone(plan: SetupPlan, key: string, tree: RepoTreeNode | null | undefined): SetupPlan {
  const zone = plan.zones.find((z) => z.key === key);
  if (!zone || splitBlocker(zone, tree) !== null) return plan;
  const [anchor] = globAnchors(zone.include[0]);
  const folder = findFolder(tree, anchor.dir);
  const kids = (folder?.children ?? []).filter((c) => c.name && !c.name.startsWith('.')).sort((a, b) => a.name.localeCompare(b.name));
  const taken = new Set(plan.zones.filter((z) => z.key !== key).map((z) => z.slug));
  const parts: PlanZone[] = kids.map((kid) => {
    const slug = uniqueSlug(kid.name, taken);
    taken.add(slug);
    return { key: `${zone.key}/${kid.name}`, slug, title: kid.name.slice(0, TITLE_MAX), include: [`${anchor.dir ? `${anchor.dir}/` : ''}${kid.name}/**`], from: zone.from };
  });
  const at = plan.zones.findIndex((z) => z.key === key);
  const zones = [...plan.zones.slice(0, at), ...parts, ...plan.zones.slice(at + 1)];
  return { zones, log: [...plan.log, `split ${zone.slug} into ${parts.map((p) => p.slug).join(', ')}`] };
}

/** Every problem that would make the server refuse the file (slug grammar, duplicates, empty globs, title length). */
export function planErrors(plan: SetupPlan): string[] {
  const errors: string[] = [];
  const seen = new Set<string>();
  for (const z of plan.zones) {
    if (!SLUG_RE.test(z.slug)) errors.push(`${z.slug || '(empty)'}: a slug is 1–48 of a-z, 0-9, - and _, starting with a letter or digit`);
    else if (z.slug === BUILTIN_SLUG) errors.push(`${z.slug}: reserved for the built-in policy zone`);
    else if (seen.has(z.slug)) errors.push(`${z.slug}: used twice`);
    seen.add(z.slug);
    if (!z.title.trim() || z.title.length > TITLE_MAX) errors.push(`${z.slug}: a title is 1–${TITLE_MAX} characters`);
    if (!z.include.length) errors.push(`${z.slug}: needs at least one folder glob`);
  }
  return errors;
}

const q = (s: string) => JSON.stringify(s);

/** zones.yml text for the plan (deterministic; parses on the server to exactly these zones). */
export function planToYaml(plan: SetupPlan): string {
  const lines = [
    '# Remembra Crew zones. Loosening changes need a human approval in the dashboard.',
    '# Saved from the dashboard setup; commit it on the default branch.',
    'version: 1',
  ];
  if (!plan.zones.length) lines.push('zones: {}');
  else {
    lines.push('zones:');
    for (const z of plan.zones) {
      lines.push(`  ${q(z.slug)}:`);
      lines.push(`    title: ${q(z.title.trim())}`);
      lines.push('    include:');
      for (const g of z.include) lines.push(`      - ${q(g)}`);
    }
  }
  return `${lines.join('\n')}\n`;
}

/** A `git apply`-able patch that creates `.remembra/zones.yml` with `yaml`. */
export function newFilePatch(yaml: string, path = ZONES_PATH): string {
  const body = yaml.endsWith('\n') ? yaml.slice(0, -1).split('\n') : yaml.split('\n');
  return [
    `diff --git a/${path} b/${path}`,
    'new file mode 100644',
    '--- /dev/null',
    `+++ b/${path}`,
    `@@ -0,0 +1,${body.length} @@`,
    ...body.map((l) => `+${l}`),
    '',
  ].join('\n');
}
