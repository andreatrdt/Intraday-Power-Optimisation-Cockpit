import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge } from "./App";
import { ConnectionStatus } from "./ConnectionStatus";
import { ProductNav } from "./ProductNav";
import { formatTimestampWithZone } from "./time";
import {
  ApiError,
  createReplayRun,
  loadReplayCumulativePnl,
  loadReplayDatasets,
  loadReplayEpisodes,
  loadReplayMetrics,
} from "./api";
import type {
  CumulativePnlPoint,
  ExecutionMode,
  IntegrityReport,
  ReplayCreateResponse,
  ReplayDatasetInfo,
  ReplayEpisodeResult,
  ReplayMetrics,
  ReplayRun,
  TraderPolicy,
} from "./types";

// SAMPLE replay is a diagnostic; it is NOT historical or live performance.
const DIAGNOSTIC_NOTE = "Replay results are diagnostic. SAMPLE replay is not historical performance.";
const POLICIES: TraderPolicy[] = ["TIMING_POLICY", "MODEL_FOLLOW", "NO_ACTION"];
const MODES: ExecutionMode[] = ["IDEAL", "REALISTIC", "STRESS"];
const POLICY_LABEL: Record<TraderPolicy, string> = {
  TIMING_POLICY: "Timing policy", MODEL_FOLLOW: "Model follow", NO_ACTION: "No action",
};

const n = (v: number | null | undefined, d = 2) =>
  v === null || v === undefined ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });
const money = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v < 0 ? "−" : "+"}£${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${n(v, 1)}%`);

interface ReplayData {
  run: ReplayRun;
  integrity: IntegrityReport;
  metrics: ReplayMetrics;
  episodes: ReplayEpisodeResult[];
  cumulative: CumulativePnlPoint[];
}

export function ReplayPage() {
  const [datasets, setDatasets] = useState<ReplayDatasetInfo[]>([]);
  const [dataset, setDataset] = useState<string>("sample-replay-v1");
  const [policy, setPolicy] = useState<TraderPolicy>("TIMING_POLICY");
  const [mode, setMode] = useState<ExecutionMode>("REALISTIC");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<ReplayData | null>(null);
  const [selected, setSelected] = useState<ReplayEpisodeResult | null>(null);
  const [lastRun, setLastRun] = useState<Date | null>(null);

  useEffect(() => { void loadReplayDatasets().then(setDatasets).catch(() => setDatasets([])); }, []);

  const run = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const created: ReplayCreateResponse = await createReplayRun({
        dataset_id: dataset, run_mode: "SAMPLE_REPLAY", trader_policy: policy, execution_mode: mode,
      });
      const rid = created.run.replay_run_id;
      const [metrics, episodes, cumulative] = await Promise.all([
        loadReplayMetrics(rid), loadReplayEpisodes(rid), loadReplayCumulativePnl(rid),
      ]);
      setData({ run: created.run, integrity: created.integrity, metrics, episodes, cumulative });
      setLastRun(new Date());
    } catch (cause) {
      const message = cause instanceof ApiError
        ? (cause.body as { detail?: { message?: string } } | undefined)?.detail?.message ?? cause.message
        : cause instanceof Error ? cause.message : "Replay failed";
      setError(message);
      setData(null);
    } finally {
      setBusy(false);
    }
  }, [dataset, policy, mode]);

  return <div className="app-shell replay-page">
    <header className="topbar">
      <div className="brand-lockup"><div className="brand-mark">IP</div><div><p className="eyebrow">UK INTRADAY POWER</p><h1>Point-in-Time Replay</h1></div></div>
      <ProductNav active="replay" />
      <ConnectionStatus error={Boolean(error)} lastPoll={lastRun} />
    </header>
    <main>
      {error && <div className="error-banner"><strong>Replay error</strong><span>{error}</span><button onClick={() => void run()}>Retry</button></div>}

      {/* A. Header + B. Configuration */}
      <section className="replay-config panel">
        <div className="replay-config-fields">
          <label><span>Dataset</span>
            <select value={dataset} onChange={(e) => setDataset(e.target.value)}>
              {datasets.map((d) => <option key={d.dataset_id} value={d.dataset_id}>{d.dataset_id}</option>)}
            </select>
          </label>
          <label><span>Trader policy</span>
            <select value={policy} onChange={(e) => setPolicy(e.target.value as TraderPolicy)}>
              {POLICIES.map((p) => <option key={p} value={p}>{POLICY_LABEL[p]}</option>)}
            </select>
          </label>
          <label><span>Execution mode</span>
            <select value={mode} onChange={(e) => setMode(e.target.value as ExecutionMode)}>
              {MODES.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
          <button className="primary-action" disabled={busy} onClick={() => void run()}>{busy ? "Running…" : "Run replay"}</button>
        </div>
        <div className="replay-badges">
          <span className={`pill ${data?.run.run_mode === "HISTORICAL_REPLAY" ? "pill-degraded" : "pill-sample"}`}>{data?.run.run_mode ?? "SAMPLE_REPLAY"}</span>
          <span className="pill pill-diagnostic">DIAGNOSTIC ONLY</span>
          <span className="pill pill-nonexec">NOT EXECUTABLE</span>
          {data && <Badge value={data.run.source_mode} />}
          {data && <Badge value={data.run.quality} />}
        </div>
        <p className="replay-disclaimer">{DIAGNOSTIC_NOTE}</p>
      </section>

      {!data && !busy && <section className="empty panel replay-empty">
        <p><strong>No replay yet.</strong> Choose a dataset, trader policy and execution mode, then <em>Run replay</em>. Every replay is deterministic, point-in-time and diagnostic — never live or historical performance.</p>
      </section>}

      {data && <>
        {/* Run header meta */}
        <section className="replay-run-meta panel">
          <div><span>Dataset</span><strong>{data.run.dataset_id}</strong></div>
          <div><span>Trader policy</span><strong>{POLICY_LABEL[data.run.trader_policy]}</strong></div>
          <div><span>Execution mode</span><strong>{data.run.execution_mode}</strong></div>
          <div><span>Replay interval</span><strong>{formatTimestampWithZone(data.run.replay_start, "UK time")} → {formatTimestampWithZone(data.run.replay_end, "UK time")}</strong></div>
          <div><span>Timing policy</span><strong>{data.run.timing_policy_version}</strong></div>
          <div><span>Trustworthy for live</span><strong>{data.run.trustworthy_for_live_trading ? "YES" : "NO"}</strong></div>
        </section>

        {/* C. Summary strip (always shows sample size) */}
        <section className="replay-summary panel">
          <div><span>Sample size</span><strong>{data.metrics.sample_size}</strong></div>
          <div><span>Eligible periods</span><strong>{data.metrics.coverage.total_eligible_periods}</strong></div>
          <div><span>Material decisions</span><strong>{data.metrics.coverage.material_decision_count}</strong></div>
          <div><span>Submitted</span><strong>{data.metrics.coverage.submitted_decision_count}</strong></div>
          <div><span>Evaluated</span><strong>{data.metrics.coverage.evaluated_count}</strong></div>
          <div><span>Skipped</span><strong>{data.metrics.coverage.skipped_count}</strong></div>
          <div><span>Total incr. P&amp;L vs no action</span><strong className="cash">{money(data.metrics.pnl.total_incremental_pnl_gbp)}</strong></div>
          <div><span>Hit rate</span><strong>{pct(data.metrics.hit_regret.pct_outperforming_no_action)}</strong></div>
          <div><span>Max drawdown</span><strong className="cash">{money(data.metrics.risk.max_drawdown_gbp)}</strong></div>
          <div><span>Fill rate</span><strong>{data.metrics.execution.fill_rate === null ? "—" : pct(data.metrics.execution.fill_rate * 100)}</strong></div>
          <div><span>Avg regret vs perfect foresight</span><strong className="cash">{money(data.metrics.hit_regret.mean_regret_vs_perfect_foresight_gbp)}</strong></div>
        </section>
        <p className="sample-size-note">{data.metrics.sample_size_note}</p>

        {/* D. Cumulative P&L chart */}
        <CumulativePnlChart points={data.cumulative} />

        {/* E. Outcome distribution */}
        <OutcomeDistribution metrics={data.metrics} episodes={data.episodes} />

        {/* F. Segmentation */}
        <Segmentation metrics={data.metrics} />

        {/* G. Episode table */}
        <section className="replay-episodes panel">
          <div className="section-heading"><div><p className="eyebrow">EPISODES</p><h2>Per-period outcomes</h2></div><span>{data.episodes.length} periods</span></div>
          <table className="episode-table">
            <thead><tr><th>SP</th><th>Action</th><th>Timing</th><th>Policy</th><th>Execution</th><th>Incr. P&amp;L</th><th>Regret vs PF</th><th>Warnings</th></tr></thead>
            <tbody>
              {data.episodes.map((e) => <tr key={e.episode_id} tabIndex={0} onClick={() => setSelected(e)} onKeyDown={(ev) => ev.key === "Enter" && setSelected(e)}>
                <td>SP{e.settlement_period}</td>
                <td>{e.recommended_action ?? "—"}</td>
                <td>{e.timing_verdict ?? "—"}</td>
                <td>{e.trader_policy_action ?? "—"}</td>
                <td>{e.skip_reason ? <span className="skip-reason">{e.skip_reason}</span> : (e.lifecycle_path ?? "—")}</td>
                <td className="cash">{e.realised_incremental_pnl_gbp === null ? "—" : money(e.realised_incremental_pnl_gbp)}</td>
                <td className="cash">{e.regret_vs_perfect_foresight_gbp === null ? "—" : money(e.regret_vs_perfect_foresight_gbp)}</td>
                <td>{e.warnings.length > 0 ? `${e.warnings.length}` : "—"}</td>
              </tr>)}
            </tbody>
          </table>
        </section>

        {/* H. Integrity panel */}
        <IntegrityPanel integrity={data.integrity} metrics={data.metrics} />
      </>}
    </main>

    {selected && <EpisodeDrawer episode={selected} onClose={() => setSelected(null)} />}
  </div>;
}

function CumulativePnlChart({ points }: { points: CumulativePnlPoint[] }) {
  const width = 720, height = 220, pad = 34;
  const series = useMemo(() => {
    if (points.length === 0) return null;
    const xs = points.map((_, i) => i);
    const all = points.flatMap((p) => [p.cumulative_trader_gbp, p.cumulative_model_gbp, p.cumulative_no_action_gbp, p.cumulative_perfect_foresight_gbp]);
    const min = Math.min(0, ...all), max = Math.max(0, ...all);
    const sx = (i: number) => pad + (xs.length <= 1 ? 0 : (i / (xs.length - 1)) * (width - 2 * pad));
    const sy = (v: number) => height - pad - (max === min ? 0 : ((v - min) / (max - min)) * (height - 2 * pad));
    const path = (key: keyof CumulativePnlPoint) => points.map((p, i) => `${i === 0 ? "M" : "L"}${sx(i).toFixed(1)},${sy(p[key] as number).toFixed(1)}`).join(" ");
    return { sx, sy, path, zeroY: sy(0) };
  }, [points]);

  return <section className="replay-chart panel">
    <div className="section-heading"><div><p className="eyebrow">CUMULATIVE P&amp;L</p><h2>Cumulative incremental P&amp;L vs no action</h2></div><span>trader · model · no action · perfect foresight (hindsight)</span></div>
    {series === null
      ? <p className="evidence-missing">No evaluated episodes to chart.</p>
      : <svg className="cumulative-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Cumulative incremental P&L">
          <line x1={pad} x2={width - pad} y1={series.zeroY} y2={series.zeroY} className="zero-line" />
          <path d={series.path("cumulative_no_action_gbp")} className="series-no-action" fill="none" />
          <path d={series.path("cumulative_model_gbp")} className="series-model" fill="none" />
          <path d={series.path("cumulative_perfect_foresight_gbp")} className="series-perfect" fill="none" strokeDasharray="5 4" />
          <path d={series.path("cumulative_trader_gbp")} className="series-trader" fill="none" />
        </svg>}
    <div className="chart-legend">
      <span className="lg lg-trader">Trader policy</span>
      <span className="lg lg-model">Model benchmark</span>
      <span className="lg lg-no-action">No action</span>
      <span className="lg lg-perfect">Perfect foresight · HINDSIGHT (not attainable)</span>
    </div>
  </section>;
}

function OutcomeDistribution({ metrics, episodes }: { metrics: ReplayMetrics; episodes: ReplayEpisodeResult[] }) {
  const evaluated = episodes.filter((e) => e.skip_reason === null && e.realised_incremental_pnl_gbp !== null);
  const sorted = [...evaluated].sort((a, b) => (a.realised_incremental_pnl_gbp ?? 0) - (b.realised_incremental_pnl_gbp ?? 0));
  const worst = sorted[0];
  const best = sorted[sorted.length - 1];
  const wins = evaluated.filter((e) => (e.realised_incremental_pnl_gbp ?? 0) > 0.01).length;
  const losses = evaluated.filter((e) => (e.realised_incremental_pnl_gbp ?? 0) < -0.01).length;
  const inLine = evaluated.length - wins - losses;
  return <section className="replay-distribution panel">
    <div className="section-heading"><div><p className="eyebrow">DISTRIBUTION</p><h2>Outcome distribution</h2></div><span>{evaluated.length} evaluated</span></div>
    <div className="distribution-counts">
      <div className="win"><span>Win</span><strong>{wins}</strong></div>
      <div className="loss"><span>Loss</span><strong>{losses}</strong></div>
      <div className="in-line"><span>In line</span><strong>{inLine}</strong></div>
      <div><span>Worst</span><strong className="cash">{worst ? money(worst.realised_incremental_pnl_gbp) : "—"}</strong></div>
      <div><span>Best</span><strong className="cash">{best ? money(best.realised_incremental_pnl_gbp) : "—"}</strong></div>
      <div><span>Median</span><strong className="cash">{money(metrics.pnl.median_incremental_pnl_gbp)}</strong></div>
    </div>
  </section>;
}

function Segmentation({ metrics }: { metrics: ReplayMetrics }) {
  const [dimension, setDimension] = useState<string>("timing_verdict");
  const dimensions = Array.from(new Set(metrics.segments.map((s) => s.dimension)));
  const rows = metrics.segments.filter((s) => s.dimension === dimension);
  return <section className="replay-segmentation panel">
    <div className="section-heading"><div><p className="eyebrow">SEGMENTATION</p><h2>Breakdown</h2></div>
      <select value={dimension} onChange={(e) => setDimension(e.target.value)}>
        {dimensions.map((d) => <option key={d} value={d}>{d.replaceAll("_", " ")}</option>)}
      </select>
    </div>
    <table className="segment-table">
      <thead><tr><th>Segment</th><th>Episodes</th><th>Evaluated</th><th>Total incr. P&amp;L</th><th>Mean incr. P&amp;L</th></tr></thead>
      <tbody>
        {rows.map((s) => <tr key={`${s.dimension}-${s.segment}`}>
          <td>{s.segment}</td><td>{s.episode_count}</td><td>{s.evaluated_count}</td>
          <td className="cash">{money(s.total_incremental_pnl_gbp)}</td>
          <td className="cash">{s.mean_incremental_pnl_gbp === null ? "—" : money(s.mean_incremental_pnl_gbp)}</td>
        </tr>)}
      </tbody>
    </table>
  </section>;
}

function IntegrityPanel({ integrity, metrics }: { integrity: IntegrityReport; metrics: ReplayMetrics }) {
  const clean = integrity.lookahead_violation_count === 0;
  return <section className="replay-integrity panel">
    <div className="section-heading"><div><p className="eyebrow">INTEGRITY</p><h2>Point-in-time integrity</h2></div>
      <span className={clean ? "integrity-ok" : "integrity-bad"}>{clean ? "Zero look-ahead violations ✓" : `${integrity.lookahead_violation_count} look-ahead violations`}</span>
    </div>
    <dl className="detail-list">
      <div><dt>Look-ahead violations</dt><dd>{integrity.lookahead_violation_count}</dd></div>
      <div><dt>Dataset validation</dt><dd>{integrity.dataset_validation_status}</dd></div>
      <div><dt>Skipped episodes</dt><dd>{metrics.coverage.skipped_count}</dd></div>
      <div><dt>Perfect-foresight capture</dt><dd>{metrics.hit_regret.perfect_foresight_capture_ratio === null ? "undefined" : n(metrics.hit_regret.perfect_foresight_capture_ratio, 3)}</dd></div>
    </dl>
    <p className="capture-note">{metrics.hit_regret.capture_ratio_note}</p>
    {integrity.violations.map((v, i) => <p className="warn-line" key={i}>{v.kind}: {v.detail}</p>)}
    {integrity.skipped_data_reasons.map((r) => <p className="warn-line" key={r}>{r}</p>)}
  </section>;
}

function EpisodeDrawer({ episode, onClose }: { episode: ReplayEpisodeResult; onClose: () => void }) {
  const decisionId = episode.decision_ids[0];
  return <div className="drawer-backdrop" onMouseDown={onClose}>
    <aside className="drawer replay-episode-drawer" onMouseDown={(e) => e.stopPropagation()}>
      <div className="drawer-head">
        <div><p className="eyebrow">EPISODE · SP{episode.settlement_period}</p><h3>{episode.delivery_period}</h3></div>
        <button onClick={onClose} aria-label="Close episode drawer">×</button>
      </div>
      <section className="drawer-section">
        <h4>Outcome</h4>
        <dl className="detail-list">
          <div><dt>Lifecycle path</dt><dd>{episode.skip_reason ?? episode.lifecycle_path ?? "—"}</dd></div>
          <div><dt>Timing verdict</dt><dd>{episode.timing_verdict ?? "—"}</dd></div>
          <div><dt>Trader policy action</dt><dd>{episode.trader_policy_action ?? "—"}</dd></div>
          <div><dt>Realised incremental P&amp;L</dt><dd className="cash">{episode.realised_incremental_pnl_gbp === null ? "—" : money(episode.realised_incremental_pnl_gbp)}</dd></div>
          <div><dt>Model / Trader / Perfect P&amp;L</dt><dd className="cash">{money(episode.model_pnl_gbp)} / {money(episode.trader_pnl_gbp)} / {money(episode.perfect_foresight_pnl_gbp)}</dd></div>
          <div><dt>Regret vs model / perfect foresight</dt><dd className="cash">{money(episode.regret_vs_model_gbp)} / {money(episode.regret_vs_perfect_foresight_gbp)}</dd></div>
        </dl>
        <p className="hindsight-banner">Perfect foresight is a <strong>HINDSIGHT UPPER BOUND — NOT ATTAINABLE</strong>.</p>
      </section>
      {decisionId && <section className="drawer-section">
        <h4>Evidence</h4>
        <p>This episode reused the production decision workflow. Open the decision queue to inspect the underlying decision, timing assessment and evaluation.</p>
        <a className="link-id" href={`/decisions#${decisionId}`}>Decision {decisionId.slice(0, 20)}…</a>
      </section>}
      {episode.warnings.map((w) => <p className="warn-line" key={w}>{w}</p>)}
    </aside>
  </div>;
}
