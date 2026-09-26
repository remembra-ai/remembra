import { describe, expect, it } from 'vitest';
import { bayer, cloudLevel, launchPackets, packetColumn } from '../dither';

describe('pixel clouds and packets', () => {
  it('uses a full 8×8 ordered-dither matrix', () => {
    const seen = new Set<number>();
    for (let j = 0; j < 8; j++) for (let i = 0; i < 8; i++) seen.add(bayer(i, j));
    expect(seen.size).toBe(64);
    expect(bayer(8, 8)).toBe(bayer(0, 0));
  });

  it('keeps the upper left (behind the title and copy) clear and banks the clouds right', () => {
    const cols = 160;
    const rows = 24;
    let left = 0;
    let right = 0;
    for (let j = 0; j < rows; j++) {
      for (let i = 0; i < 40; i++) if (j < rows / 2) left += cloudLevel(i, j, cols, rows, 0);
      for (let i = cols - 40; i < cols; i++) right += cloudLevel(i, j, cols, rows, 0);
    }
    expect(left).toBe(0);
    expect(right).toBeGreaterThan(40);
    for (let i = 0; i < cols; i += 7) for (let j = 0; j < rows; j += 3) expect([0, 1, 2, 3]).toContain(cloudLevel(i, j, cols, rows, 12.5));
  });

  it('launches one packet per new event (at most four) and runs each across once', () => {
    expect(launchPackets(10, 10, 0)).toEqual([]);
    expect(launchPackets(10, 9, 0)).toEqual([]);
    const burst = launchPackets(10, 30, 1000);
    expect(burst).toHaveLength(4);
    expect(burst.map((p) => p.born)).toEqual([1000, 1180, 1360, 1540]);
    const p = { born: 0, duration: 1000 };
    expect(packetColumn(p, -1, 100)).toBeNull();
    expect(packetColumn(p, 0, 100)).toBe(-2);
    expect(packetColumn(p, 500, 100)).toBeGreaterThan(50); // ease-out: past half-way at half time
    expect(packetColumn(p, 1000, 100)).toBe(100);
    expect(packetColumn(p, 1001, 100)).toBeNull();
  });
});
