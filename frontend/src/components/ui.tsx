"use client";

/**
 * Shared UI primitives (Phase 1 UI spec section 8): StatTile, DataTable,
 * Modal, ConfirmDialog, Badge, ProgressBar, Toast. Kept in one file for a
 * Phase-1-sized app; split up if/when the component count grows in a later
 * phase.
 */

import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

// --- Badge ---------------------------------------------------------------

export type BadgeTone = "green" | "gray" | "amber" | "red";

export function Badge({
  tone,
  children,
  title,
}: {
  tone: BadgeTone;
  children: ReactNode;
  /** Native tooltip on hover - used sparingly, e.g. to disclose a known
   * limitation alongside a status badge (see `audit-entries.tsx`'s hash-
   * chain integrity badge) without adding a second UI element. */
  title?: string;
}) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

// --- Stat tile -------------------------------------------------------------

export function StatTile({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="stat-tile">
      <div className="stat-tile-label">{label}</div>
      <div className="stat-tile-value">{value}</div>
      {hint ? <div className="stat-tile-hint">{hint}</div> : null}
    </div>
  );
}

export function StatTileSkeleton() {
  return (
    <div className="stat-tile">
      <div className="skeleton skeleton-text" style={{ width: "60%" }} />
      <div className="skeleton skeleton-text" style={{ width: "40%", height: 24, marginTop: 8 }} />
    </div>
  );
}

// --- Progress / budget bar --------------------------------------------------

export function BudgetBar({ spend, budget }: { spend: number; budget: number | null }) {
  if (budget === null) {
    return <span className="text-muted">Unmetered</span>;
  }
  const pct = budget > 0 ? Math.min((spend / budget) * 100, 100) : 100;
  const tone = spend >= budget ? "red" : pct >= 80 ? "amber" : "green";
  return (
    <div className="budget-bar" title={`$${spend.toFixed(2)} / $${budget.toFixed(2)}`}>
      <div className={`budget-bar-fill budget-bar-${tone}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

// --- Data table --------------------------------------------------------------

export interface Column<T> {
  key: string;
  header: string;
  align?: "left" | "right";
  render: (row: T) => ReactNode;
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  emptyState,
  loading,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  emptyState: ReactNode;
  loading?: boolean;
}) {
  return (
    <table className="data-table">
      <thead>
        <tr>
          {columns.map((c) => (
            <th key={c.key} className={c.align === "right" ? "align-right" : undefined}>
              {c.header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {loading ? (
          Array.from({ length: 4 }).map((_, i) => (
            <tr key={`skeleton-${i}`}>
              {columns.map((c) => (
                <td key={c.key}>
                  <div className="skeleton skeleton-text" />
                </td>
              ))}
            </tr>
          ))
        ) : rows.length === 0 ? (
          <tr>
            <td colSpan={columns.length} className="empty-state-cell">
              {emptyState}
            </td>
          </tr>
        ) : (
          rows.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((c) => (
                <td key={c.key} className={c.align === "right" ? "align-right" : undefined}>
                  {c.render(row)}
                </td>
              ))}
            </tr>
          ))
        )}
      </tbody>
    </table>
  );
}

// --- Modal / confirm dialog --------------------------------------------------

export function Modal({
  title,
  onClose,
  children,
  closeOnBackdropClick = true,
  width = 480,
}: {
  title: string;
  onClose: (() => void) | null;
  children: ReactNode;
  closeOnBackdropClick?: boolean;
  width?: number;
}) {
  return (
    <div
      className="modal-backdrop"
      onClick={() => {
        if (closeOnBackdropClick && onClose) onClose();
      }}
    >
      <div className="modal" style={{ maxWidth: width }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{title}</h2>
          {onClose ? (
            <button className="modal-close" onClick={onClose} aria-label="Close">
              &times;
            </button>
          ) : null}
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

export function ConfirmDialog({
  title,
  consequence,
  confirmLabel,
  destructive = true,
  onConfirm,
  onCancel,
  busy,
}: {
  title: string;
  consequence: string;
  confirmLabel: string;
  destructive?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  busy?: boolean;
}) {
  return (
    <Modal title={title} onClose={onCancel}>
      <p className="confirm-consequence">{consequence}</p>
      <div className="modal-actions">
        <button className="btn btn-secondary" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button
          className={destructive ? "btn btn-danger" : "btn btn-primary"}
          onClick={onConfirm}
          disabled={busy}
        >
          {busy ? "Working..." : confirmLabel}
        </button>
      </div>
    </Modal>
  );
}

// --- Inline field error -------------------------------------------------------

export function FieldError({ message }: { message?: string | null }) {
  if (!message) return null;
  return <div className="field-error">{message}</div>;
}

// --- Toast --------------------------------------------------------------------

interface ToastItem {
  id: number;
  kind: "success" | "error";
  message: string;
}

interface ToastContextValue {
  push: (kind: "success" | "error", message: string) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

let toastIdCounter = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const push = useCallback((kind: "success" | "error", message: string) => {
    const id = ++toastIdCounter;
    setToasts((prev) => [...prev, { id, kind, message }]);
    if (kind === "success") {
      setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
      }, 4000);
    }
  }, []);

  const dismiss = (id: number) => setToasts((prev) => prev.filter((t) => t.id !== id));

  return (
    <ToastContext.Provider value={{ push }}>
      {children}
      <div className="toast-stack">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`} onClick={() => dismiss(t.id)}>
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within a ToastProvider");
  return ctx;
}
