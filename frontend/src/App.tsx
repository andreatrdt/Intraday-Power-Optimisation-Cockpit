import { useCallback, useEffect, useMemo, useState } from "react";
import { loadCockpit, loadLineage, refreshFeed } from "./api";
import type {
  CanonicalDataPoint,
  CockpitSnapshot,
  DataFlowEvent,
  FeedHealth,
  LineageResponse,
  Quality,
  Readiness,
  SemanticKind,
  SourceMode,
} from "./types";

const SOURCE_MODES: SourceMode[] = ["LIVE", "LATEST_AVAILABLE", "SAMPLE", "SYNTHETIC", "ERROR"];
const QUALITIES: Quality[] = ["FRESH", "STALE", "PARTIAL", "MISSING", "REVISED", "INVALID"];
const PIPELINE_STAGES = ["SOURCE", "INGESTION", "RAW_PAYLOAD", "NORMALISATION", "VALIDATION", "CANONICAL", "SNAPSHOT"];

export function App() {
  const [snapshot, setSnapshot] = useState<CockpitSnapshot | null>(null);
  const [feeds, setFeeds] = useState<FeedHealth[]>([]);
  const [events, setEvents] = useState<DataFlowEvent[]>([]);
  const [selected, setSelected] = useState<LineageResponse | null>(null);
  const [refreshing, setRefreshing] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [lastLoaded, setLastLoaded] = useState<Date | null>(null);

  const reload = useCallback(async (quiet = false) => {
    try {
      const data = await loadCockpit();
      setSnapshot(data.snapshot);
      setFeeds(data.feeds);
      setEvents(data.events);
      setLastLoaded(new Date());
      setError(null);
    } catch (cause) {
      if (!quiet) setError(cause instanceof Error ? cause.message : "Unable to load cockpit API");
    }
  }, []);

  useEffect(() => {
    void reload();
    const timer = window.setInterval(() => void reload(true), 5000);
    return () => window.clearInterval(timer);
  }, [reload]);

  const handleRefresh = async (feedId: string) => {
    setRefreshing((current) => new Set(current).add(feedId));
    try {
      await refreshFeed(feedId);
      await reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : `Refresh failed for ${feedId}`);
      await reload(true);
    } finally {
      setRefreshing((current) => {
        const next = new Set(current);
        next.delete(feedId);
        return next;
      });
    }
  };

  const openLineage = async (point: CanonicalDataPoint) => {
    try {
      setSelected(await loadLineage(point.value_id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to load value lineage");
    }
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-mark">IP</div>
          <div>
            <p className="eyebrow">UK INTRADAY POWER</p>
            <h1>Data Flow Control Room</h1>
          </div>
        </div>
        <nav>
          <a className="active" href="/data-flow">Data flow</a>
          <a href="/forecast-position">Forecast &amp; position</a>
          <a href="/market-liquidity">Market &amp; liquidity</a>
          <span>Optimisation</span>
          <span>Actions</span>
        </nav>
        <div className="connection">
          <span className={`connection-dot ${error ? "down" : ""}`} />
          <span>{error ? "API issue" : "API connected"}</span>
          <small>{lastLoaded ? time(lastLoaded.toISOString()) : "connecting…"}</small>
        </div>
      </header>

      <main>
        <section className="hero-row">
          <div>
            <p className="eyebrow">MILESTONE 1A · LIVE DATA FLOW OBSERVABILITY</p>
            <h2>Trust the recommendation only when you can trace every input.</h2>
            <p className="intro">
              Source → ingestion → normalisation → validation → canonical value → snapshot → optimiser readiness
            </p>
          </div>
          {snapshot && (
            <div className="readiness-strip">
              <ReadinessBlock title="Cockpit snapshot" status={snapshot.status} />
              <ReadinessBlock title="Optimiser" status={snapshot.optimiser_readiness.status} />
            </div>
          )}
        </section>

        {error && <div className="error-banner"><strong>API error</strong><span>{error}</span><button onClick={() => void reload()}>Retry</button></div>}

        <section className="legend panel">
          <div><span className="legend-label">Source mode</span>{SOURCE_MODES.map((item) => <Badge key={item} value={item} />)}</div>
          <div><span className="legend-label">Quality</span>{QUALITIES.map((item) => <Badge key={item} value={item} />)}</div>
        </section>

        <SectionHeading
          eyebrow="01 · CONNECTIVITY"
          title="Source feeds"
          aside={`${feeds.filter((feed) => feed.connected).length}/${feeds.length} currently connected`}
        />
        <section className="feed-grid">
          {feeds.map((feed) => (
            <FeedCard
              key={feed.feed_id}
              feed={feed}
              busy={refreshing.has(feed.feed_id)}
              onRefresh={() => void handleRefresh(feed.feed_id)}
            />
          ))}
        </section>

        <section className="split-layout">
          <div>
            <SectionHeading eyebrow="02 · PROCESS TRACE" title="Refresh event log" aside="Newest first · polls every 5s" />
            <EventTimeline events={events} />
          </div>
          <div>
            <SectionHeading eyebrow="03 · DECISION GATE" title="Current snapshot inspector" />
            {snapshot ? <SnapshotInspector snapshot={snapshot} /> : <EmptyPanel label="Waiting for a snapshot…" />}
          </div>
        </section>

        <SectionHeading
          eyebrow="04 · CANONICAL INPUTS"
          title="Values entering the current cockpit snapshot"
          aside="Click any row to inspect its lineage"
        />
        {snapshot ? (
          <CanonicalTable values={snapshot.values} onSelect={(point) => void openLineage(point)} />
        ) : (
          <EmptyPanel label="No canonical values available." />
        )}
      </main>

      {selected && <LineageDrawer response={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

function ReadinessBlock({ title, status }: { title: string; status: Readiness }) {
  return (
    <div className="readiness-block">
      <span>{title}</span>
      <strong className={`readiness ${status.toLowerCase()}`}>{status}</strong>
    </div>
  );
}

function SectionHeading({ eyebrow, title, aside }: { eyebrow: string; title: string; aside?: string }) {
  return (
    <div className="section-heading">
      <div><p className="eyebrow">{eyebrow}</p><h3>{title}</h3></div>
      {aside && <span>{aside}</span>}
    </div>
  );
}

function FeedCard({ feed, busy, onRefresh }: { feed: FeedHealth; busy: boolean; onRefresh: () => void }) {
  const stageIndex = Math.max(0, PIPELINE_STAGES.indexOf(feed.pipeline_stage));
  const isSynthetic = feed.feed_id === "synthetic_demo";
  return (
    <article className={`feed-card ${feed.source_mode.toLowerCase()}`}>
      <div className="feed-card-head">
        <div>
          <span className={`feed-signal ${feed.connected ? "connected" : ""}`} />
          <h4>{feed.feed_name}</h4>
        </div>
        <div className="badges"><Badge value={feed.source_mode} /><Badge value={feed.quality} /></div>
      </div>
      <p className="feed-description">{feed.description}</p>
      <div className="kind-row"><span>Semantic kind</span><Badge value={feed.semantic_kind} /></div>
      <div className="flow-track" title={`Current stage: ${feed.pipeline_stage}`}>
        {PIPELINE_STAGES.map((stage, index) => (
          <span key={stage} className={index <= stageIndex ? "passed" : ""} title={stage} />
        ))}
      </div>
      <div className="flow-labels"><span>Source</span><span>Canonical</span><span>Snapshot</span></div>
      <dl className="feed-stats">
        <Stat label="Last attempt" value={dateTime(feed.last_refresh_attempt)} />
        <Stat label="Last success" value={dateTime(feed.last_successful_refresh)} />
        <Stat label="Cadence / SLA" value={`${duration(feed.expected_refresh_cadence_seconds)} / ${duration(feed.freshness_sla_seconds)}`} />
        <Stat label="Age" value={age(feed.age_seconds)} />
        <Stat label="Retrieved / canonical" value={`${feed.rows_retrieved} / ${feed.rows_normalised}`} />
        <Stat label="Retry" value={feed.retry_status} />
      </dl>
      {feed.latest_error_message && <div className="feed-error">{feed.latest_error_message}</div>}
      {feed.validation_errors.length > 0 && <div className="feed-error">{feed.validation_errors.length} validation errors</div>}
      <div className="feed-footer">
        <span className={feed.included_in_current_snapshot ? "included" : "excluded"}>
          {feed.included_in_current_snapshot ? "● Included in snapshot" : "○ Excluded from snapshot"}
        </span>
        <button disabled={busy} onClick={onRefresh}>
          {busy ? "Refreshing…" : isSynthetic ? "Load explicitly" : "Refresh now"}
        </button>
      </div>
    </article>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>;
}

function EventTimeline({ events }: { events: DataFlowEvent[] }) {
  if (!events.length) return <EmptyPanel label="No data-flow events yet." />;
  return (
    <div className="timeline panel">
      {events.slice(0, 30).map((event) => (
        <div className={`timeline-row ${event.level.toLowerCase()}`} key={event.event_id}>
          <time>{time(event.occurred_at)}</time>
          <span className="timeline-node" />
          <div>
            <div className="timeline-meta"><span>{event.stage.replaceAll("_", " ")}</span>{event.feed_id && <code>{event.feed_id}</code>}</div>
            <p>{event.message}</p>
          </div>
        </div>
      ))}
    </div>
  );
}

function SnapshotInspector({ snapshot }: { snapshot: CockpitSnapshot }) {
  return (
    <article className="snapshot-card panel">
      <div className="snapshot-status">
        <div><span>Snapshot status</span><strong className={`readiness ${snapshot.status.toLowerCase()}`}>{snapshot.status}</strong></div>
        <div><span>Optimiser gate</span><strong className={`readiness ${snapshot.optimiser_readiness.status.toLowerCase()}`}>{snapshot.optimiser_readiness.status}</strong></div>
      </div>
      <dl className="snapshot-meta">
        <Stat label="Snapshot ID" value={snapshot.snapshot_id} />
        <Stat label="As of" value={dateTime(snapshot.as_of)} />
        <Stat label="Input hash" value={`${snapshot.input_hash.slice(0, 16)}…`} />
        <Stat label="Canonical values" value={String(snapshot.values.length)} />
      </dl>
      <ReasonBlock title="Snapshot decision" reasons={snapshot.readiness.reasons} />
      <ReasonBlock title="Optimiser decision" reasons={snapshot.optimiser_readiness.reasons} danger={!snapshot.optimiser_readiness.allowed} />
      <div className="feed-lists">
        <FeedList title="Included" feeds={snapshot.feeds_included} tone="good" />
        <FeedList title="Excluded" feeds={snapshot.feeds_excluded} tone="muted" />
        <FeedList title="Missing / invalid" feeds={snapshot.missing_feeds} tone="bad" />
      </div>
    </article>
  );
}

function ReasonBlock({ title, reasons, danger = false }: { title: string; reasons: string[]; danger?: boolean }) {
  return <div className={`reason-block ${danger ? "danger" : ""}`}><strong>{title}</strong>{reasons.map((reason) => <p key={reason}>{reason}</p>)}</div>;
}

function FeedList({ title, feeds, tone }: { title: string; feeds: string[]; tone: string }) {
  return <div><span>{title}</span><div>{feeds.length ? feeds.map((feed) => <code className={tone} key={feed}>{feed}</code>) : <em>None</em>}</div></div>;
}

function CanonicalTable({ values, onSelect }: { values: CanonicalDataPoint[]; onSelect: (point: CanonicalDataPoint) => void }) {
  const sorted = useMemo(
    () => [...values].sort((a, b) => `${a.delivery_start ?? ""}:${a.metric}`.localeCompare(`${b.delivery_start ?? ""}:${b.metric}`)),
    [values],
  );
  return (
    <div className="table-wrap panel">
      <table>
        <thead><tr><th>Delivery period</th><th>Canonical metric</th><th>Value</th><th>Source</th><th>Mode</th><th>Kind</th><th>Quality</th><th>Retrieved</th></tr></thead>
        <tbody>
          {sorted.map((point) => (
            <tr key={point.value_id} onClick={() => onSelect(point)} tabIndex={0} onKeyDown={(event) => event.key === "Enter" && onSelect(point)}>
              <td>{point.delivery_period ?? "Current state"}</td>
              <td><strong>{metricLabel(point.metric)}</strong><small>{point.value_id.slice(0, 8)}</small></td>
              <td className="value-cell">{formatValue(point.value)} <span>{point.unit}</span></td>
              <td><code>{point.lineage.source_feed}</code></td>
              <td><Badge value={point.lineage.source_mode} /></td>
              <td><Badge value={point.lineage.semantic_kind} /></td>
              <td><Badge value={point.lineage.quality} /></td>
              <td>{time(point.lineage.retrieved_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function LineageDrawer({ response, onClose }: { response: LineageResponse; onClose: () => void }) {
  const point = response.value;
  return (
    <div className="drawer-backdrop" onMouseDown={onClose}>
      <aside className="drawer" onMouseDown={(event) => event.stopPropagation()}>
        <div className="drawer-head">
          <div><p className="eyebrow">VALUE LINEAGE</p><h3>{metricLabel(point.metric)}</h3><p>{point.delivery_period ?? "Current state"}</p></div>
          <button onClick={onClose} aria-label="Close lineage drawer">×</button>
        </div>
        <div className="hero-value"><strong>{formatValue(point.value)}</strong><span>{point.unit}</span></div>
        <div className="badges drawer-badges"><Badge value={point.lineage.source_mode} /><Badge value={point.lineage.semantic_kind} /><Badge value={point.lineage.quality} /></div>
        <section className="drawer-section">
          <h4>Identity &amp; snapshot</h4>
          <dl className="detail-list">
            <Stat label="Value ID" value={point.value_id} />
            <Stat label="Source feed" value={point.lineage.source_feed} />
            <Stat label="Snapshot ID" value={point.snapshot_id ?? "Not included"} />
            <Stat label="Included" value={point.included_in_current_snapshot ? "Yes" : "No"} />
            <Stat label="Previous value" value={point.previous_value === null ? "No prior refresh" : String(point.previous_value)} />
            <Stat label="Delta" value={point.delta_vs_previous === null ? "n/a" : String(point.delta_vs_previous)} />
          </dl>
        </section>
        <section className="drawer-section">
          <h4>Time trace</h4>
          <dl className="detail-list">
            <Stat label="Published" value={dateTime(point.lineage.published_at)} />
            <Stat label="Retrieved" value={dateTime(point.lineage.retrieved_at)} />
            <Stat label="Normalised" value={dateTime(point.lineage.normalised_at)} />
            <Stat label="Age at snapshot" value={age(response.age_seconds)} />
            <Stat label="Delivery start" value={dateTime(point.delivery_start)} />
          </dl>
        </section>
        <section className="drawer-section">
          <h4>Raw → canonical transformation</h4>
          <p className="raw-field">Raw field <code>{point.lineage.raw_field_name}</code></p>
          <ol className="transform-list">{point.lineage.transformations.map((item) => <li key={item}>{item}</li>)}</ol>
        </section>
        <section className="drawer-section">
          <h4>Validation</h4>
          {point.lineage.validation_checks.map((check) => (
            <div className={`check ${check.passed ? "passed" : "failed"}`} key={check.name}>
              <strong>{check.passed ? "✓" : "×"} {check.name}</strong><span>{check.detail}</span>
            </div>
          ))}
        </section>
        {point.lineage.warnings.length > 0 && <section className="drawer-section warnings"><h4>Warnings</h4>{point.lineage.warnings.map((warning) => <p key={warning}>{warning}</p>)}</section>}
      </aside>
    </div>
  );
}

export function Badge({ value }: { value: SourceMode | SemanticKind | Quality }) {
  return <span className={`badge badge-${value.toLowerCase()}`}>{value.replaceAll("_", " ")}</span>;
}

function EmptyPanel({ label }: { label: string }) {
  return <div className="empty panel">{label}</div>;
}

function metricLabel(metric: string): string {
  const labels: Record<string, string> = {
    wind_p10: "Wind P10",
    wind_p50: "Wind P50",
    wind_p90: "Wind P90",
    contracted_position_q: "Contracted position Qₜ",
    battery_soc: "Battery state of charge",
    gb_system_frequency: "GB system frequency",
    neso_system_dataset_matches: "NESO system datasets discovered",
    upward_service_commitment: "Upward service commitment",
    downward_service_commitment: "Downward service commitment",
    synthetic_diagnostic_price: "Synthetic diagnostic price",
  };
  return labels[metric] ?? metric.replaceAll("_", " ");
}

function formatValue(value: number | string | boolean): string {
  return typeof value === "number" ? value.toLocaleString(undefined, { maximumFractionDigits: 3 }) : String(value);
}

function dateTime(value: string | null): string {
  if (!value) return "Never";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" }).format(new Date(value));
}

function time(value: string): string {
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
}

function age(seconds: number | null): string {
  if (seconds === null) return "n/a";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function duration(seconds: number): string {
  if (seconds === 0) return "manual";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${seconds / 60}m`;
  return `${seconds / 3600}h`;
}
