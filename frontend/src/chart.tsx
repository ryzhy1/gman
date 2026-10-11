import { useEffect, useRef, useState } from 'react';

/** Categorical slots in fixed order (validated: CVD/normal-vision separation on white). */
export const SERIES_COLORS = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'];

export type Series = { name: string; color: string; points: [number, number | null][] };

const HEIGHT = 180;
const PAD = { top: 10, right: 12, bottom: 22, left: 56 };

function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  useEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

function niceMax(v: number, binary: boolean) {
  if (v <= 0) return 1;
  if (binary) {  // Byte axes: 512 MiB, 1 GiB, 2 GiB… instead of 954 MiB.
    const magnitude = 1024 ** Math.floor(Math.log(v) / Math.log(1024));
    return [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024].map(m => m * magnitude).find(m => m >= v)!;
  }
  const magnitude = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 2.5, 5, 10].map(m => m * magnitude).find(m => m >= v)!;
}

const time = (t: number, span: number) => new Date(t * 1000).toLocaleString('ru-RU',
  span > 86400 ? { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' } : { hour: '2-digit', minute: '2-digit' });

/** Line chart, one y-axis; a crosshair snaps to the nearest timestamp and lists every series. */
export function LineChart({ title, series, format, note, binary = false }: {
  title: string; series: Series[]; format: (v: number) => string; note?: string; binary?: boolean;
}) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const times = [...new Set(series.flatMap(s => s.points.map(p => p[0])))].sort((a, b) => a - b);
  const values = series.flatMap(s => s.points.map(p => p[1]).filter((v): v is number => v != null));
  const hasData = values.length > 0;
  const [t0, t1] = [times[0] ?? 0, times[times.length - 1] ?? 1];
  const yMax = niceMax(Math.max(0, ...values), binary);
  const plotW = Math.max(50, width - PAD.left - PAD.right), plotH = HEIGHT - PAD.top - PAD.bottom;
  const x = (t: number) => PAD.left + (t1 === t0 ? plotW / 2 : (t - t0) / (t1 - t0) * plotW);
  const y = (v: number) => PAD.top + plotH - v / yMax * plotH;
  const path = (s: Series) => {
    let d = '', pen = false;
    for (const [t, v] of s.points) {
      if (v == null) { pen = false; continue; }  // Gaps stay gaps: missing data is not zero.
      d += `${pen ? 'L' : 'M'}${x(t).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    }
    return d;
  };
  const latest = (s: Series) => [...s.points].reverse().find(p => p[1] != null)?.[1] ?? null;
  const at = (s: Series, t: number) => s.points.find(p => p[0] === t)?.[1] ?? null;

  function move(e: React.PointerEvent<SVGRectElement>) {
    const box = e.currentTarget.getBoundingClientRect();
    const t = t0 + (e.clientX - box.left) / box.width * (t1 - t0);
    setHover(times.reduce((best, c) => Math.abs(c - t) < Math.abs(best - t) ? c : best, times[0]));
  }

  return <figure className="chart" ref={ref}>
    <figcaption><b>{title}</b>{note && <small>{note}</small>}</figcaption>
    {!hasData ? <div className="chart-empty">Нет данных за период</div> :
      <div className="chart-plot">
        <svg width={width} height={HEIGHT} role="img" aria-label={title}>
          {[0, .5, 1].map(f => <g key={f}>
            <line className="grid" x1={PAD.left} x2={PAD.left + plotW} y1={y(yMax * f)} y2={y(yMax * f)} />
            <text className="axis" x={PAD.left - 6} y={y(yMax * f) + 4} textAnchor="end">{format(yMax * f)}</text>
          </g>)}
          <text className="axis" x={PAD.left} y={HEIGHT - 4}>{time(t0, t1 - t0)}</text>
          <text className="axis" x={PAD.left + plotW} y={HEIGHT - 4} textAnchor="end">{time(t1, t1 - t0)}</text>
          {series.map(s => <path key={s.name} d={path(s)} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" />)}
          {hover != null && <>
            <line className="crosshair" x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={PAD.top + plotH} />
            {series.map(s => { const v = at(s, hover); return v == null ? null :
              <circle key={s.name} cx={x(hover)} cy={y(v)} r={4} fill={s.color} stroke="#fff" strokeWidth={2} />; })}
          </>}
          <rect x={PAD.left} y={PAD.top} width={plotW} height={plotH} fill="transparent"
                onPointerMove={move} onPointerLeave={() => setHover(null)} />
        </svg>
        {hover != null && <div className="tooltip" style={{ left: Math.min(x(hover) + 12, width - 220) }}>
          <b>{time(hover, 0)}</b>
          {series.map(s => <div key={s.name}><i style={{ background: s.color }} />{s.name}<span>{at(s, hover) == null ? '—' : format(at(s, hover)!)}</span></div>)}
        </div>}
      </div>}
    {series.length > 1 && <ul className="legend">{series.map(s => <li key={s.name}>
      <i style={{ background: s.color }} />{s.name}<span>{latest(s) == null ? '—' : format(latest(s)!)}</span>
    </li>)}</ul>}
  </figure>;
}
