"use client";

/**
 * Shared rotation-policy form (Phase 3, BD-15, design doc section 9.6).
 * Reused for the org-wide default (app/compliance/rotation), per-key
 * override (service-accounts page), and provider-key rotation reminder
 * (providers page) - `mode` always renders read-only (server-determined,
 * never a client-writable field per the backend schema's own docstring).
 */

import { useState } from "react";
import { FieldError, useToast } from "@/components/ui";
import { ApiError, type RotationPolicyPutBody, type RotationPolicyResponse } from "@/lib/api";
import { inputValueToTime, timeToInputValue } from "@/lib/time";

export function RotationPolicyForm({
  policy,
  onSave,
  onSaved,
  inheritHint,
}: {
  policy: RotationPolicyResponse;
  onSave: (body: RotationPolicyPutBody) => Promise<RotationPolicyResponse>;
  onSaved?: (updated: RotationPolicyResponse) => void;
  /** Per-key/provider-key overrides only - leaving interval blank inherits
   * the org default (the org-default scope itself has nothing to inherit
   * from, so its caller omits this). */
  inheritHint?: string;
}) {
  const toast = useToast();
  const [enabled, setEnabled] = useState(policy.enabled);
  const [interval, setInterval_] = useState(policy.interval_days?.toString() ?? "");
  const [localTime, setLocalTime] = useState(timeToInputValue(policy.rotate_at_local_time));
  const [overlap, setOverlap] = useState(policy.overlap_buffer_minutes.toString());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      const result = await onSave({
        enabled,
        interval_days: interval.trim() ? Number(interval) : null,
        rotate_at_local_time: inputValueToTime(localTime),
        overlap_buffer_minutes: Number(overlap) || 5,
      });
      toast.push("success", "Rotation policy saved.");
      onSaved?.(result);
    } catch (err) {
      // 422 rotation_interval_required surfaces verbatim (enabling without
      // an interval on a scope with nothing further up the chain).
      setError(err instanceof ApiError ? err.message : "Failed to save rotation policy.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          id="rot-enabled"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
          style={{ width: "auto" }}
        />
        <label htmlFor="rot-enabled" style={{ margin: 0 }}>
          Enable scheduled rotation ({policy.mode === "automatic" ? "fully automatic" : "guided, manual"})
        </label>
      </div>
      <div className="field">
        <label>Interval (days)</label>
        <input
          type="text"
          value={interval}
          onChange={(e) => setInterval_(e.target.value)}
          placeholder={inheritHint ? "Blank = inherit the org default" : "e.g. 90"}
          disabled={!enabled}
        />
        {inheritHint ? <div className="field-hint">{inheritHint}</div> : null}
      </div>
      <div className="field">
        <label>Rotate at (local time, optional)</label>
        <input
          type="time"
          value={localTime}
          onChange={(e) => setLocalTime(e.target.value)}
          disabled={!enabled}
        />
      </div>
      <div className="field">
        <label>Overlap buffer (minutes)</label>
        <input type="text" value={overlap} onChange={(e) => setOverlap(e.target.value)} disabled={!enabled} />
        <div className="field-hint">
          The old secret keeps working for this long after a rotation - a short overlap, never
          an immediate cutover, so in-flight callers don&apos;t break.
        </div>
      </div>
      {policy.last_rotated_at || policy.next_rotation_at ? (
        <p className="text-muted" style={{ fontSize: 12 }}>
          {policy.last_rotated_at ? `Last rotated ${new Date(policy.last_rotated_at).toLocaleString()}. ` : ""}
          {policy.next_rotation_at ? `Next rotation ${new Date(policy.next_rotation_at).toLocaleString()}.` : ""}
        </p>
      ) : null}
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
          {busy ? "Saving..." : "Save rotation policy"}
        </button>
      </div>
    </div>
  );
}
