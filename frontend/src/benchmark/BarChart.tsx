/** A small horizontal bar chart drawn as plain SVG (no charting dependency). */

export interface Bar {
  label: string;
  value: number;
  /** Text shown at the end of the bar. */
  text: string;
}

interface Props {
  title: string;
  bars: Bar[];
  /** Value that fills the whole bar width; defaults to the largest bar. */
  max?: number;
}

const LABEL_WIDTH = 112;
const BAR_AREA = 200;
const VALUE_WIDTH = 64;
const ROW = 26;
const BAR_HEIGHT = 16;

export function BarChart({ title, bars, max }: Props) {
  const scale = max ?? Math.max(...bars.map((b) => b.value), 0);
  const width = LABEL_WIDTH + BAR_AREA + VALUE_WIDTH;
  const height = bars.length * ROW + 4;
  return (
    <figure className="bar-chart">
      <figcaption>{title}</figcaption>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title}>
        {bars.map((bar, i) => {
          const y = i * ROW + 2;
          const length = scale > 0 ? Math.max((bar.value / scale) * BAR_AREA, bar.value > 0 ? 2 : 0) : 0;
          return (
            <g key={bar.label} data-approach={bar.label}>
              <text x={LABEL_WIDTH - 8} y={y + BAR_HEIGHT - 4} textAnchor="end" className="bar-label">
                {bar.label}
              </text>
              <rect x={LABEL_WIDTH} y={y} width={BAR_AREA} height={BAR_HEIGHT} rx={3} className="bar-track" />
              <rect x={LABEL_WIDTH} y={y} width={length} height={BAR_HEIGHT} rx={3} className="bar-fill" />
              <text x={LABEL_WIDTH + BAR_AREA + 8} y={y + BAR_HEIGHT - 4} className="bar-value">
                {bar.text}
              </text>
            </g>
          );
        })}
      </svg>
    </figure>
  );
}
