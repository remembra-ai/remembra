/**
 * Stable color palette for brain-layer communities (themes).
 *
 * Each detected community gets a distinct, vivid hue so the knowledge graph
 * reads as clusters of related memory at a glance. Entities with no community
 * (singletons / not-yet-analyzed) fall back to a neutral slate.
 */

// Hand-picked for contrast on a dark canvas and against each other.
const COMMUNITY_PALETTE = [
  '#a855f7', // violet
  '#22d3ee', // cyan
  '#f59e0b', // amber
  '#34d399', // emerald
  '#f472b6', // pink
  '#60a5fa', // blue
  '#fb7185', // rose
  '#a3e635', // lime
  '#c084fc', // light purple
  '#2dd4bf', // teal
  '#fbbf24', // gold
  '#818cf8', // indigo
];

export const UNCLUSTERED_COLOR = '#64748b'; // slate-500

/** Deterministic color for a community id (null → neutral). */
export function communityColor(communityId: number | null | undefined): string {
  if (communityId === null || communityId === undefined) {
    return UNCLUSTERED_COLOR;
  }
  const idx = ((communityId % COMMUNITY_PALETTE.length) + COMMUNITY_PALETTE.length) % COMMUNITY_PALETTE.length;
  return COMMUNITY_PALETTE[idx];
}

export { COMMUNITY_PALETTE };
