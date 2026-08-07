"use client";

/**
 * Shared access-schedule form (Phase 3, BD-16/17, design doc section 9.7).
 * Reused for the org default (app/compliance/access-windows), team-level
 * narrowing (team detail page), and per-key narrowing (service-accounts
 * page) - each caller wires its own get/put/delete against the matching
 * scope's endpoints; this component only renders the shared field set.
 */

import { useState } from "react";
import { FieldError, useToast } from "@/components/ui";
import { ApiError, type AccessSchedulePutBody, type AccessScheduleResponse } from "@/lib/api";
import { inputValueToTime, timeToInputValue } from "@/lib/time";

const DAY_LABELS: [number, string][] = [
  [1, "Mon"],
  [2, "Tue"],
  [3, "Wed"],
  [4, "Thu"],
  [5, "Fri"],
  [6, "Sat"],
  [7, "Sun"],
];

export function AccessScheduleForm({
  schedule,
  onSave,
  onRemoveOverride,
  timezoneLabel,
  narrowingHint,
}: {
  /** null = unrestricted (no row yet, the normal default state per AC9.3). */
  schedule: AccessScheduleResponse | null;
  onSave: (body: AccessSchedulePutBody) => Promise<AccessScheduleResponse>;
  /** Team/per-key scopes only - removes the narrowing row so the parent
   * schedule applies again. Absent for the org-wide screen (nothing above it). */
  onRemoveOverride?: () => Promise<void>;
  timezoneLabel?: string;
  narrowingHint?: string;
}) {
  const toast = useToast();
  const [enabled, setEnabled] = useState(schedule?.enabled ?? false);
  const [days, setDays] = useState<Set<number>>(new Set(schedule?.allowed_days ?? []));
  const [start, setStart] = useState(timeToInputValue(schedule?.allowed_hours_start ?? null));
  const [end, setEnd] = useState(timeToInputValue(schedule?.allowed_hours_end ?? null));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggleDay(day: number) {
    setDays((prev) => {
      const next = new Set(prev);
      if (next.has(day)) next.delete(day);
      else next.add(day);
      return next;
    });
  }

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      await onSave({
        enabled,
        allowed_days: [...days],
        allowed_hours_start: inputValueToTime(start),
        allowed_hours_end: inputValueToTime(end),
      });
      toast.push("success", "Access schedule saved.");
    } catch (err) {
      // 422 access_schedule_widens_parent passes through verbatim (AC9.2
      // defense-in-depth - a narrowing scope can never re-widen its parent).
      setError(err instanceof ApiError ? err.message : "Failed to save access schedule.");
    } finally {
      setBusy(false);
    }
  }

  async function handleRemove() {
    if (!onRemoveOverride) return;
    setBusy(true);
    setError(null);
    try {
      await onRemoveOverride();
      setEnabled(false);
      setDays(new Set());
      setStart("");
      setEnd("");
      toast.push("success", "Override removed - the parent schedule applies again.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to remove the override.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      {narrowingHint ? <p className="field-hint">{narrowingHint}</p> : null}
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          id="sched-enabled"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
          style={{ width: "auto" }}
        />
        <label htmlFor="sched-enabled" style={{ margin: 0 }}>
          Restrict access to this window{timezoneLabel ? ` (${timezoneLabel})` : ""}
        </label>
      </div>
      <div className="field">
        <label>Allowed days</label>
        <div style={{ display: "flex", gap: 10 }}>
          {DAY_LABELS.map(([value, label]) => (
            <label key={value} style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <input
                type="checkbox"
                checked={days.has(value)}
                onChange={() => toggleDay(value)}
                disabled={!enabled}
                style={{ width: "auto" }}
              />
              {label}
            </label>
          ))}
        </div>
      </div>
      <div className="field" style={{ display: "flex", gap: 12 }}>
        <div style={{ flex: 1 }}>
          <label>Allowed hours start</label>
          <input type="time" value={start} onChange={(e) => setStart(e.target.value)} disabled={!enabled} />
        </div>
        <div style={{ flex: 1 }}>
          <label>Allowed hours end</label>
          <input type="time" value={end} onChange={(e) => setEnd(e.target.value)} disabled={!enabled} />
        </div>
      </div>
      <div className="field-hint">Leave both hours blank to allow the full day on each allowed day.</div>
      <FieldError message={error} />
      <div className="modal-actions" style={{ justifyContent: onRemoveOverride ? "space-between" : "flex-end" }}>
        {onRemoveOverride && schedule ? (
          <button className="btn btn-secondary" onClick={handleRemove} disabled={busy}>
            Remove override
          </button>
        ) : onRemoveOverride ? (
          <span />
        ) : null}
        <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
          {busy ? "Saving..." : "Save schedule"}
        </button>
      </div>
    </div>
  );
}
