import { formatRow } from "./format";

export interface Sample {
  coord: string;
  score: number;
}

export type Verdict = "keep" | "review" | "drop";

export class Report {
  private rows: Sample[] = [];

  constructor(private threshold: number) {}

  add(s: Sample): void {
    this.rows.push(s);
  }

  // Classify a sample: keep above threshold, review at half, else drop.
  verdict(s: Sample): Verdict {
    if (s.score >= this.threshold) return "keep";
    if (s.score >= this.threshold / 2) return "review";
    return "drop";
  }

  render(): string {
    return this.rows.map((r) => formatRow(r.coord, this.verdict(r))).join("\n");
  }
}

export const summarize = (rs: Sample[]): number =>
  rs.length === 0 ? 0 : rs.reduce((a, r) => a + r.score, 0) / rs.length;
