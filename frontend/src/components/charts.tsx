"use client";

/**
 * SVG chart primitives - no chart library, themed via the CSS tokens in
 * globals.css (--chart-1, --chart-grid), so light/dark mode "just work".
 *
 * Design rules baked in (do not undo casually):
 * - 2px line, round join/cap; area fill is the series hue at 10% opacity.
 * - Solid hairline gridlines, one step off the surface; never dashed.
 * - Single series -> no legend (the panel title names it); the endpoint
 *   carries the one direct label, everything else lives on the axis, the
 *   crosshair tooltip, and the caller-provided table view.
 * - Tooltips enhance, never gate: callers must keep a non-hover path to the
 *   values (SpendDayTable below is the ready-made table twin).
 * - The crosshair snaps to the nearest data point; the whole plot is the
 *   hit target, and keyboard focus + arrow keys reach every point.
 */

import { useCallback, useMemo, useRef, useState } from "react";

export interface TimePoint {
  date: string;
  value: number;
}

// viewBox coordinate system; the SVG scales to its container width.
const VB_W = 640;
const VB_H = 210;
const PAD_LEFT = 46;
const PAD_RIGHT = 62; // room for the endpoint's direct label
const PAD_TOP = 14;
const PAD_BOTTOM = 26; // x-axis label band - inside the viewBox, never clipped

function niceTicks(maxValue: number): number[] {
  if (maxValue <= 0) return [0, 1];
  const rawStep = maxValue / 4;
  const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= rawStep) ?? 10 * mag;
  const ticks: number[] = [];
  for (let v = 0; v <= maxValue + step * 0.999; v += step) ticks.push(v);
  return ticks;
}

function shortDate(iso: string): string {
  // "2026-08-15" -> "Aug 15" (fall back to the raw string for odd inputs).
  const d = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

const defaultFormat = (v: number) => `$${v.toFixed(2)}`;

export function SpendOverTimeChart({
  points,
  label,
  formatValue = defaultFormat,
}: {
  points: TimePoint[];
  /** Accessible name for the plot, e.g. "Spend by day". */
  label: string;
  formatValue?: (v: number) => string;
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [active, setActive] = useState<number | null>(null);

  const geometry = useMemo(() => {
    const values = points.map((p) => p.value);
    const maxValue = Math.max(1e-9, ...values);
    const ticks = niceTicks(maxValue);
    const yMax = ticks[ticks.length - 1];
    const plotW = VB_W - PAD_LEFT - PAD_RIGHT;
    const plotH = VB_H - PAD_TOP - PAD_BOTTOM;
    const x = (i: number) =>
      points.length <= 1 ? PAD_LEFT + plotW / 2 : PAD_LEFT + (i / (points.length - 1)) * plotW;
    const y = (v: number) => PAD_TOP + plotH - (v / yMax) * plotH;
    const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(p.value)}`).join(" ");
    const baseline = PAD_TOP + plotH;
    const areaPath =
      points.length > 1
        ? `${linePath} L${x(points.length - 1)},${baseline} L${x(0)},${baseline} Z`
        : "";
    // ~5 x labels, always including first and last.
    const stride = Math.max(1, Math.ceil(points.length / 5));
    const xLabelIdx = points.map((_, i) => i).filter((i) => i % stride === 0 || i === points.length - 1);
    return { ticks, yMax, x, y, linePath, areaPath, baseline, xLabelIdx };
  }, [points]);

  const pickNearest = useCallback(
    (clientX: number) => {
      const el = wrapRef.current;
      if (!el || points.length === 0) return null;
      const rect = el.getBoundingClientRect();
      const xView = ((clientX - rect.left) / rect.width) * VB_W;
      let best = 0;
      let bestDist = Infinity;
      for (let i = 0; i < points.length; i++) {
        const d = Math.abs(geometry.x(i) - xView);
        if (d < bestDist) {
          bestDist = d;
          best = i;
        }
      }
      return best;
    },
    [points, geometry],
  );

  if (points.length === 0) {
    return <div className="chart-empty">No spend data for this range.</div>;
  }

  const { ticks, x, y, linePath, areaPath, baseline, xLabelIdx } = geometry;
  const lastIdx = points.length - 1;
  const activePoint = active !== null ? points[active] : null;

  return (
    <div className="chart-wrap" ref={wrapRef}>
      <svg
        className="chart-svg"
        viewBox={`0 0 ${VB_W} ${VB_H}`}
        role="img"
        aria-label={`${label}: ${points.length} points from ${points[0].date} to ${points[lastIdx].date}. Latest ${formatValue(points[lastIdx].value)}. Use the table view for exact values.`}
        tabIndex={0}
        onPointerMove={(e) => setActive(pickNearest(e.clientX))}
        onPointerLeave={() => setActive(null)}
        onFocus={() => setActive((a) => a ?? lastIdx)}
        onBlur={() => setActive(null)}
        onKeyDown={(e) => {
          if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
            e.preventDefault();
            setActive((a) => {
              const cur = a ?? lastIdx;
              return e.key === "ArrowLeft" ? Math.max(0, cur - 1) : Math.min(lastIdx, cur + 1);
            });
          } else if (e.key === "Home") {
            e.preventDefault();
            setActive(0);
          } else if (e.key === "End") {
            e.preventDefault();
            setActive(lastIdx);
          } else if (e.key === "Escape") {
            setActive(null);
          }
        }}
      >
        {/* gridlines + y tick labels */}
        {ticks.map((t) => (
          <g key={t}>
            <line className="chart-grid-line" x1={PAD_LEFT} x2={VB_W - PAD_RIGHT} y1={y(t)} y2={y(t)} />
            <text className="chart-axis-text" x={PAD_LEFT - 6} y={y(t) + 3} textAnchor="end">
              {t >= 1 ? `$${Math.round(t).toLocaleString()}` : `$${t.toFixed(2)}`}
            </text>
          </g>
        ))}
        {/* x labels */}
        {xLabelIdx.map((i) => (
          <text
            key={points[i].date}
            className="chart-axis-text"
            x={x(i)}
            y={baseline + 16}
            textAnchor={i === 0 ? "start" : i === lastIdx ? "end" : "middle"}
          >
            {shortDate(points[i].date)}
          </text>
        ))}
        {/* crosshair (snapped to the active point) */}
        {activePoint !== null && active !== null ? (
          <line
            className="chart-crosshair"
            x1={x(active)}
            x2={x(active)}
            y1={PAD_TOP}
            y2={baseline}
          />
        ) : null}
        {/* area + line */}
        {areaPath ? <path className="chart-area" d={areaPath} /> : null}
        <path className="chart-line" d={linePath} />
        {/* active point marker */}
        {activePoint !== null && active !== null && active !== lastIdx ? (
          <circle className="chart-dot" cx={x(active)} cy={y(activePoint.value)} r={4} />
        ) : null}
        {/* endpoint: marker with surface ring + the one direct label */}
        <circle className="chart-dot" cx={x(lastIdx)} cy={y(points[lastIdx].value)} r={4} />
        <text
          className="chart-end-label"
          x={x(lastIdx) + 8}
          y={y(points[lastIdx].value) + 4}
        >
          {formatValue(points[lastIdx].value)}
        </text>
      </svg>
      {activePoint !== null && active !== null ? (
        <div
          className="chart-tooltip"
          style={{
            left: `${(x(active) / VB_W) * 100}%`,
            top: 0,
            transform: x(active) > VB_W * 0.72 ? "translateX(calc(-100% - 10px))" : "translateX(10px)",
          }}
        >
          <div className="tooltip-value">{formatValue(activePoint.value)}</div>
          <div className="tooltip-label">{shortDate(activePoint.date)}</div>
        </div>
      ) : null}
    </div>
  );
}

/** The chart's table twin - every plotted value, reachable without hover. */
export function SpendDayTable({
  points,
  formatValue = defaultFormat,
}: {
  points: TimePoint[];
  formatValue?: (v: number) => string;
}) {
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>Date</th>
            <th className="align-right">Spend</th>
          </tr>
        </thead>
        <tbody>
          {points.map((p) => (
            <tr key={p.date}>
              <td>{p.date}</td>
              <td className="align-right">{formatValue(p.value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Panel body combining the chart with a chart/table view toggle. */
export function SpendOverTimePanel({
  points,
  label,
  formatValue = defaultFormat,
}: {
  points: TimePoint[];
  label: string;
  formatValue?: (v: number) => string;
}) {
  const [view, setView] = useState<"chart" | "table">("chart");
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 6 }}>
        <button
          type="button"
          className="btn-link"
          style={{ fontSize: 12 }}
          onClick={() => setView((v) => (v === "chart" ? "table" : "chart"))}
          aria-pressed={view === "table"}
        >
          {view === "chart" ? "View as table" : "View as chart"}
        </button>
      </div>
      {view === "chart" ? (
        <SpendOverTimeChart points={points} label={label} formatValue={formatValue} />
      ) : (
        <SpendDayTable points={points} formatValue={formatValue} />
      )}
    </div>
  );
}
