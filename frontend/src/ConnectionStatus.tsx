import { formatLocalTime, type TimestampInput } from "./time";

export function ConnectionStatus({ error, lastPoll }: { error: boolean; lastPoll: TimestampInput | null }) {
  return <div className="connection" data-testid="global-connection-status" aria-live="polite">
    <span className={`connection-dot ${error ? "down" : ""}`} />
    <span>{error ? "Backend unavailable" : "Backend connected"}</span>
    <small>{lastPoll ? `Last poll: ${formatLocalTime(lastPoll)} local time` : "Last poll: awaiting response (local time)"}</small>
  </div>;
}
