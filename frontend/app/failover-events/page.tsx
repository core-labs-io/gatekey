"use client";

/**
 * Failover Events admin console (Phase 4, Reliability & Cost Efficiency,
 * AC4.1.8, referenced by AC4.5.7). Read-only, Org Admin only. Filterable by
 * date range, same `from`/`to` half-open convention as the Audit Log
 * screen.
 *
 * `from_provider_key_id`/`to_provider_key_id` are resolved to their
 * `"label (Provider)"` via `listProviderKeys()` (Phase 4), fetched alongside
 * the events themselves. A key can be deleted after an event references it
 * (`deleteProviderKeyById`/`deleteProviderKey` don't touch historical
 * `failover_events` rows), so lookup falls back to the truncated raw id
 * whenever a referenced key isn't in the current list rather than crashing
 * or showing a blank cell.
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { DataTable, useToast } from "@/components/ui";
import {
  ApiError,
  listFailoverEvents,
  listProviderKeys,
  PROVIDER_LABELS,
  type FailoverEvent,
  type ProviderKeyListItem,
} from "@/lib/api";

// NFR: failover switch time must be under 2 seconds (detection to switch).
const NFR_SWITCH_MS = 2000;

function switchDurationMs(event: FailoverEvent): number {
  return new Date(event.switched_at).getTime() - new Date(event.detected_at).getTime();
}

function shortId(id: string | null): string {
  if (!id) return "—";
  return id.length > 8 ? `${id.slice(0, 8)}…` : id;
}

/** Resolves a `from_provider_key_id`/`to_provider_key_id` to
 * `"label (Provider)"` via the fetched key list. Falls back to the
 * truncated raw id when the key was since deleted (not in `keysById`) -
 * historical events still reference it, but there's nothing left to
 * label it with. */
function keyDisplay(id: string | null, keysById: Map<string, ProviderKeyListItem>): string {
  if (!id) return "—";
  const key = keysById.get(id);
  if (!key) return shortId(id);
  return `${key.label} (${PROVIDER_LABELS[key.provider]})`;
}

export default function FailoverEventsPage() {
  const toast = useToast();
  const [events, setEvents] = useState<FailoverEvent[] | null>(null);
  const [keys, setKeys] = useState<ProviderKeyListItem[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");

  function refresh() {
    setLoading(true);
    setError(null);
    Promise.all([
      listFailoverEvents({
        from: from ? new Date(from).toISOString() : undefined,
        to: to ? new Date(to).toISOString() : undefined,
        limit: 200,
      }),
      listProviderKeys(),
    ])
      .then(([eventRows, keyRows]) => {
        setEvents(eventRows);
        setKeys(keyRows);
      })
      .catch((err) => {
        setError(err instanceof ApiError ? err.message : "Failed to load failover events.");
        toast.push("error", "Failed to load failover events.");
      })
      .finally(() => setLoading(false));
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(refresh, []);

  const keysById = new Map((keys ?? []).map((k) => [k.id, k]));

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Failover Events</div>
        <div className="page-subtitle">
          One row per successful backup-key switch (a single request retrying across multiple
          backup keys still counts as one event, AC4.5.7). Switch time flagged in red when it
          exceeds the 2-second NFR target.
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}

        <div className="page-header-row">
          <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>From</label>
              <input type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>To</label>
              <input type="datetime-local" value={to} onChange={(e) => setTo(e.target.value)} />
            </div>
            <button className="btn btn-secondary" onClick={refresh}>
              Apply filter
            </button>
          </div>
        </div>

        <DataTable
          loading={loading}
          rows={events ?? []}
          rowKey={(e) => e.id}
          emptyState="No failover events recorded for this range."
          columns={[
            { key: "detected", header: "Detected at", render: (e) => new Date(e.detected_at).toLocaleString() },
            { key: "switched", header: "Switched at", render: (e) => new Date(e.switched_at).toLocaleString() },
            {
              key: "duration",
              header: "Switch time",
              align: "right",
              render: (e) => {
                const ms = switchDurationMs(e);
                const over = ms > NFR_SWITCH_MS;
                return (
                  <span style={over ? { color: "var(--red)", fontWeight: 600 } : undefined}>
                    {ms}ms{over ? " (over 2s NFR)" : ""}
                  </span>
                );
              },
            },
            {
              key: "from_key",
              header: "From key",
              render: (e) => <span className="mono">{keyDisplay(e.from_provider_key_id, keysById)}</span>,
            },
            {
              key: "to_key",
              header: "To key",
              render: (e) => <span className="mono">{keyDisplay(e.to_provider_key_id, keysById)}</span>,
            },
            { key: "request", header: "Request", render: (e) => <span className="mono">{shortId(e.request_id)}</span> },
          ]}
        />
      </div>
    </ConsoleShell>
  );
}
