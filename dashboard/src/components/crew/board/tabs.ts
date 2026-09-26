// Tab strip helpers for the phone layouts (a horizontal strip that scrolls on its own).

/** Scroll a horizontal tab strip (not the page) so its `aria-selected` tab is fully visible. */
export function revealSelectedTab(strip: HTMLElement, margin = 16): void {
  const tab = strip.querySelector<HTMLElement>('[aria-selected="true"]');
  if (!tab) return;
  const box = strip.getBoundingClientRect();
  const at = tab.getBoundingClientRect();
  if (at.right > box.right) strip.scrollLeft += at.right - box.right + margin;
  else if (at.left < box.left) strip.scrollLeft -= box.left - at.left + margin;
}
