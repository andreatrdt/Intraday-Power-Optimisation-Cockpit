import { historyWindowLabels, type CustomWindow, type HistoryWindow } from "./historyWindow";

export function HistoryWindowSelector({ value, onChange, custom, onCustomChange }: {
  value: HistoryWindow;
  onChange: (value: HistoryWindow) => void;
  custom: CustomWindow;
  onCustomChange: (value: CustomWindow) => void;
}) {
  return <div className="history-window" aria-label="Historical context window">
    <span>Historical context</span>
    <div className="history-window-options">
      {(Object.keys(historyWindowLabels) as HistoryWindow[]).map((item) => <button type="button" key={item} aria-pressed={value === item} onClick={() => onChange(item)}>{historyWindowLabels[item]}</button>)}
    </div>
    {value === "custom" && <div className="custom-window-fields">
      <label>From<input type="datetime-local" value={custom.from} onChange={(event) => onCustomChange({ ...custom, from: event.target.value })} /></label>
      <label>To<input type="datetime-local" value={custom.to} onChange={(event) => onCustomChange({ ...custom, to: event.target.value })} /></label>
    </div>}
  </div>;
}

