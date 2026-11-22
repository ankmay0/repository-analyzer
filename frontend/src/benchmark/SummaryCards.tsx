import { BarChart } from "./BarChart";
import { formatPercent, formatSeconds, formatTokens, type ApproachSummary } from "./model";

export function SummaryCards({ summaries }: { summaries: ApproachSummary[] }) {
  if (summaries.length === 0) return null;
  return (
    <section aria-label="Summary">
      <div className="summary-cards">
        {summaries.map((s) => (
          <article key={s.approach} className="summary-card" data-approach={s.approach}>
            <h3>
              <span className="approach-swatch" aria-hidden="true" /> {s.approach}
            </h3>
            <p className="summary-big">{formatPercent(s.correctPct)}</p>
            <p className="muted">
              correct · {s.pass} pass, {s.partial} partial, {s.fail} fail of {s.answered}
            </p>
            <dl>
              <div>
                <dt>Avg tokens</dt>
                <dd>{formatTokens(s.avgTokens)}</dd>
              </div>
              <div>
                <dt>Avg latency</dt>
                <dd>{formatSeconds(s.avgLatencyMs)}</dd>
              </div>
              <div>
                <dt>Facts found</dt>
                <dd>{formatPercent(s.factPct)}</dd>
              </div>
            </dl>
          </article>
        ))}
      </div>
      <div className="charts">
        <BarChart
          title="Correct answers"
          max={100}
          bars={summaries.map((s) => ({ label: s.approach, value: s.correctPct, text: formatPercent(s.correctPct) }))}
        />
        <BarChart
          title="Average tokens per answer"
          bars={summaries.map((s) => ({ label: s.approach, value: s.avgTokens, text: formatTokens(s.avgTokens) }))}
        />
        <BarChart
          title="Average latency"
          bars={summaries.map((s) => ({ label: s.approach, value: s.avgLatencyMs, text: formatSeconds(s.avgLatencyMs) }))}
        />
      </div>
    </section>
  );
}
