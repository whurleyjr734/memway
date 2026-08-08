export function formatRow(coord: string, verdict: string): string {
  return `${pad(coord, 12)} ${verdict}`;
}

function pad(s: string, n: number): string {
  return s.length >= n ? s : s + " ".repeat(n - s.length);
}
