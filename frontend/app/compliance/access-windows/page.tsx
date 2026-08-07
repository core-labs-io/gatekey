"use client";

/**
 * Scheduled Access Windows (Phase 3, BD-16/17/18, design doc section 9.7).
 * Org default schedule + flat holiday-date list + the AC9.10 effective-
 * schedule listing per key. Team-level narrowing lives on each team's
 * detail page; per-key narrowing, and the emergency-override grant/revoke
 * UI, live on the Service Accounts screen next to each app-key row (the
 * override targets one specific key, so it's grouped with that key's other
 * actions rather than duplicated here).
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { AccessScheduleForm } from "@/components/access-schedule";
import { ConfirmDialog, DataTable, FieldError, useToast } from "@/components/ui";
import {
  ApiError,
  createHolidayDate,
  deleteHolidayDate,
  deleteOrgAccessSchedule,
  getComplianceSettings,
  getOrgAccessSchedule,
  listHolidayDates,
  listKeySchedules,
  putOrgAccessSchedule,
  type AccessScheduleResponse,
  type EffectiveScheduleEntry,
  type HolidayDateResponse,
} from "@/lib/api";

function HolidayDatesPanel() {
  const toast = useToast();
  const [rows, setRows] = useState<HolidayDateResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [date, setDate] = useState("");
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState<HolidayDateResponse | null>(null);

  function refresh() {
    setLoading(true);
    listHolidayDates()
      .then((data) => setRows([...data].sort((a, b) => a.holiday_date.localeCompare(b.holiday_date))))
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load holiday dates."))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);

  async function handleAdd() {
    setBusy(true);
    setError(null);
    try {
      await createHolidayDate({ holiday_date: date, label: label.trim() || null });
      setDate("");
      setLabel("");
      toast.push("success", "Holiday date added.");
      refresh();
    } catch (err) {
      // 409 holiday_date_already_exists passes through verbatim.
      setError(err instanceof ApiError ? err.message : "Failed to add holiday date.");
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(row: HolidayDateResponse) {
    setBusy(true);
    try {
      await deleteHolidayDate(row.id);
      toast.push("success", "Holiday date removed.");
      setDeleting(null);
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to remove holiday date.");
      setDeleting(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-title">Holiday dates</div>
      <p className="text-muted">
        A flat, org-wide list of specific dates that always block access, regardless of any
        schedule&apos;s allowed days/hours.
      </p>
      <div className="field" style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
        <div style={{ flex: 1 }}>
          <label>Date</label>
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        </div>
        <div style={{ flex: 2 }}>
          <label>Label (optional)</label>
          <input type="text" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. New Year's Day" />
        </div>
        <button className="btn btn-primary" onClick={handleAdd} disabled={busy || !date}>
          + Add
        </button>
      </div>
      <FieldError message={error} />
      <DataTable
        loading={loading}
        rows={rows}
        rowKey={(r) => r.id}
        emptyState="No holiday dates configured."
        columns={[
          { key: "date", header: "Date", render: (r) => r.holiday_date },
          { key: "label", header: "Label", render: (r) => r.label ?? <span className="text-muted">&mdash;</span> },
          {
            key: "actions",
            header: "",
            align: "right",
            render: (r) => (
              <button className="btn-link" style={{ color: "var(--red)" }} onClick={() => setDeleting(r)}>
                Delete
              </button>
            ),
          },
        ]}
      />
      {deleting ? (
        <ConfirmDialog
          title={`Delete ${deleting.holiday_date}?`}
          consequence="This cannot be undone."
          confirmLabel="Delete"
          busy={busy}
          onCancel={() => setDeleting(null)}
          onConfirm={() => handleDelete(deleting)}
        />
      ) : null}
    </div>
  );
}

function EffectiveSchedulesPanel() {
  const [rows, setRows] = useState<EffectiveScheduleEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listKeySchedules()
      .then(setRows)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load effective schedules."));
  }, []);

  return (
    <div className="panel">
      <div className="panel-title">Effective schedule per key</div>
      <p className="text-muted">
        The fully-resolved schedule for every service-account key, after applying org, team, and
        per-key narrowing.
      </p>
      {error ? <div className="banner banner-error">{error}</div> : null}
      <DataTable
        loading={rows === null && !error}
        rows={rows ?? []}
        rowKey={(r) => r.service_account_id}
        emptyState="No service-account keys yet."
        columns={[
          { key: "name", header: "Key", render: (r) => r.name },
          { key: "effective", header: "Effective schedule", render: (r) => r.effective },
        ]}
      />
    </div>
  );
}

export default function AccessWindowsPage() {
  const [schedule, setSchedule] = useState<AccessScheduleResponse | null>(null);
  const [timezone, setTimezone] = useState("UTC");
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([getOrgAccessSchedule(), getComplianceSettings()])
      .then(([sched, settings]) => {
        setSchedule(sched);
        setTimezone(settings.access_schedule_timezone);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load access schedule."))
      .finally(() => setLoaded(true));
  }, []);

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Scheduled Access Windows</div>
        <div className="page-subtitle">
          Off by default at every level. Teams can narrow the org default (never widen it) from
          their own detail page; individual keys narrow further from{" "}
          <Link href="/service-accounts">Service Accounts</Link>, which is also where emergency
          overrides are granted and revoked.
        </div>
        {error ? <div className="banner banner-error">{error}</div> : null}
        {loaded ? (
          <div className="panel">
            <div className="panel-title">Org default</div>
            <AccessScheduleForm
              key={schedule ? "configured" : "unrestricted"}
              schedule={schedule}
              onSave={async (body) => {
                const result = await putOrgAccessSchedule(body);
                setSchedule(result);
                return result;
              }}
              timezoneLabel={timezone}
            />
            {schedule ? (
              <button
                className="btn btn-secondary"
                style={{ marginTop: -8 }}
                onClick={() =>
                  deleteOrgAccessSchedule().then(() => setSchedule(null))
                }
              >
                Remove org default (unrestricted)
              </button>
            ) : null}
          </div>
        ) : (
          <div className="skeleton skeleton-text" style={{ height: 160 }} />
        )}
        <HolidayDatesPanel />
        <EffectiveSchedulesPanel />
      </div>
    </ConsoleShell>
  );
}
