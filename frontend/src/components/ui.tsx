"use client";

/**
 * Shared UI primitives: StatTile, DataTable, Modal, ConfirmDialog, Badge,
 * ProgressBar, Toast, ThemeToggle. Kept in one file while the component
 * count stays modest; split if it grows.
 *
 * Accessibility contract (applies to every consumer automatically):
 * - Modal: role="dialog" + aria-modal, labelled by its title, focus is
 *   trapped inside, Escape closes, focus returns to the opener, and the
 *   page behind doesn't scroll.
 * - Toasts: announced via a polite live region (errors via role="alert"),
 *   dismissible with a real button.
 * - DataTable: column sorting is exposed with aria-sort; the table sits in
 *   a horizontal scroll container so narrow screens never overflow the page.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

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
  const label = `$${spend.toFixed(2)} of $${budget.toFixed(2)} spent`;
  return (
    <div
      className="budget-bar"
      title={label}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={budget}
      aria-valuenow={Math.min(spend, budget)}
    >
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
  /** Present = the column is sortable (client-side). Return the value to
   * compare; strings compare case-insensitively, null/undefined sort last. */
  sortValue?: (row: T) => string | number | null | undefined;
}

function compareValues(
  a: string | number | null | undefined,
  b: string | number | null | undefined,
): number {
  const aMissing = a === null || a === undefined;
  const bMissing = b === null || b === undefined;
  if (aMissing && bMissing) return 0;
  if (aMissing) return 1; // missing values sort last in either direction
  if (bMissing) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { sensitivity: "base", numeric: true });
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  emptyState,
  loading,
  searchText,
  searchPlaceholder = "Filter...",
  pageSize = 25,
  initialSort,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  emptyState: ReactNode;
  loading?: boolean;
  /** Provide a haystack per row to enable the text-filter box. */
  searchText?: (row: T) => string;
  searchPlaceholder?: string;
  /** Client-side pagination threshold. Pass null to disable (e.g. when the
   * caller already paginates server-side). Controls only appear when the
   * row count actually exceeds the page size. */
  pageSize?: number | null;
  initialSort?: { key: string; dir: "asc" | "desc" };
}) {
  const [sort, setSort] = useState<{ key: string; dir: "asc" | "desc" } | null>(
    initialSort ?? null,
  );
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const filterId = useId();

  const filtered = useMemo(() => {
    if (!searchText || !query.trim()) return rows;
    const q = query.trim().toLowerCase();
    return rows.filter((r) => searchText(r).toLowerCase().includes(q));
  }, [rows, query, searchText]);

  const sorted = useMemo(() => {
    if (!sort) return filtered;
    const col = columns.find((c) => c.key === sort.key);
    if (!col?.sortValue) return filtered;
    const sv = col.sortValue;
    const dir = sort.dir === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => dir * compareValues(sv(a), sv(b)));
  }, [filtered, sort, columns]);

  const paginate = pageSize !== null && sorted.length > pageSize;
  const totalPages = paginate ? Math.ceil(sorted.length / pageSize) : 1;
  const safePage = Math.min(page, totalPages);
  const visible = paginate ? sorted.slice((safePage - 1) * pageSize, safePage * pageSize) : sorted;

  function toggleSort(col: Column<T>) {
    if (!col.sortValue) return;
    setPage(1);
    setSort((prev) =>
      prev?.key !== col.key
        ? { key: col.key, dir: "asc" }
        : prev.dir === "asc"
          ? { key: col.key, dir: "desc" }
          : null,
    );
  }

  return (
    <div>
      {searchText ? (
        <div className="table-toolbar">
          <input
            id={filterId}
            type="text"
            className="table-filter"
            placeholder={searchPlaceholder}
            aria-label={searchPlaceholder}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setPage(1);
            }}
          />
          {query ? (
            <span className="text-muted" style={{ fontSize: 12 }}>
              {sorted.length} of {rows.length} rows
            </span>
          ) : null}
        </div>
      ) : null}
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              {columns.map((c) => {
                const sortable = Boolean(c.sortValue);
                const active = sort?.key === c.key;
                return (
                  <th
                    key={c.key}
                    className={c.align === "right" ? "align-right" : undefined}
                    aria-sort={
                      sortable
                        ? active
                          ? sort!.dir === "asc"
                            ? "ascending"
                            : "descending"
                          : "none"
                        : undefined
                    }
                  >
                    {sortable ? (
                      <button type="button" className="th-sort-btn" onClick={() => toggleSort(c)}>
                        {c.header}
                        <span className="sort-arrow" aria-hidden>
                          {active ? (sort!.dir === "asc" ? "▲" : "▼") : "⇅"}
                        </span>
                      </button>
                    ) : (
                      c.header
                    )}
                  </th>
                );
              })}
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
            ) : visible.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="empty-state-cell">
                  {query ? "No rows match the filter." : emptyState}
                </td>
              </tr>
            ) : (
              visible.map((row) => (
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
      </div>
      {paginate ? (
        <div className="table-pagination">
          <span>
            {(safePage - 1) * pageSize + 1}&ndash;{Math.min(safePage * pageSize, sorted.length)} of{" "}
            {sorted.length}
          </span>
          <button
            type="button"
            className="btn btn-secondary"
            disabled={safePage <= 1}
            onClick={() => setPage(safePage - 1)}
          >
            Previous
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            disabled={safePage >= totalPages}
            onClick={() => setPage(safePage + 1)}
          >
            Next
          </button>
        </div>
      ) : null}
    </div>
  );
}

// --- Modal / confirm dialog --------------------------------------------------

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

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
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    const opener = document.activeElement as HTMLElement | null;

    // Initial focus: the first focusable control, else the dialog itself.
    const first = dialog.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? dialog).focus();

    // The page behind a modal doesn't scroll.
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCloseRef.current?.();
        return;
      }
      if (e.key !== "Tab") return;
      // Focus trap: Tab cycles within the dialog.
      const nodes = Array.from(dialog!.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );
      if (nodes.length === 0) {
        e.preventDefault();
        return;
      }
      const firstNode = nodes[0];
      const lastNode = nodes[nodes.length - 1];
      if (e.shiftKey && document.activeElement === firstNode) {
        e.preventDefault();
        lastNode.focus();
      } else if (!e.shiftKey && document.activeElement === lastNode) {
        e.preventDefault();
        firstNode.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      document.body.style.overflow = prevOverflow;
      opener?.focus?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keep the latest onClose reachable from the keydown listener without
  // re-binding the effect (which would re-run initial-focus logic).
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  return (
    <div
      className="modal-backdrop"
      onClick={() => {
        if (closeOnBackdropClick && onClose) onClose();
      }}
    >
      <div
        ref={dialogRef}
        className="modal"
        style={{ maxWidth: width }}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2 id={titleId}>{title}</h2>
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
      <div className="toast-stack" aria-live="polite" aria-atomic="false">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`toast toast-${t.kind}`}
            role={t.kind === "error" ? "alert" : "status"}
          >
            <span>{t.message}</span>
            <button
              type="button"
              className="toast-dismiss"
              aria-label="Dismiss notification"
              onClick={() => dismiss(t.id)}
            >
              &times;
            </button>
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

// --- Theme toggle ---------------------------------------------------------------

type ThemePref = "system" | "light" | "dark";

function readThemePref(): ThemePref {
  try {
    const v = localStorage.getItem("gatekey-theme");
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

function applyThemePref(pref: ThemePref) {
  try {
    if (pref === "system") {
      localStorage.removeItem("gatekey-theme");
      delete document.documentElement.dataset.theme;
    } else {
      localStorage.setItem("gatekey-theme", pref);
      document.documentElement.dataset.theme = pref;
    }
  } catch {
    // localStorage unavailable (private mode etc.) - theme just won't persist.
  }
}

const THEME_ICONS: Record<ThemePref, string> = {
  system: "◑", // half circle
  light: "☀︎", // sun
  dark: "☾", // moon
};
const NEXT_THEME: Record<ThemePref, ThemePref> = {
  system: "light",
  light: "dark",
  dark: "system",
};

/** Cycles system -> light -> dark. Rendered in the console topbar; safe to
 * drop anywhere (reads/writes localStorage + the <html> data-theme attr). */
export function ThemeToggle() {
  const [pref, setPref] = useState<ThemePref>("system");

  useEffect(() => {
    setPref(readThemePref());
  }, []);

  function cycle() {
    const next = NEXT_THEME[pref];
    applyThemePref(next);
    setPref(next);
  }

  const label = `Theme: ${pref}. Switch to ${NEXT_THEME[pref]}.`;
  return (
    <button type="button" className="icon-btn" onClick={cycle} aria-label={label} title={label}>
      <span aria-hidden>{THEME_ICONS[pref]}</span>
    </button>
  );
}
