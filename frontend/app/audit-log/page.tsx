"use client";

/**
 * Audit Log (Phase 2 FE-8, admin UI doc section 10.3's Phase 2 slice only -
 * plain append-only log, NO hash-chain badge / verify / export, those are
 * later phases). The filters+table live in the shared AuditEntriesView
 * (also rendered by the Auditor Org Logs screen).
 */

import { ConsoleShell } from "@/components/ConsoleShell";
import { AuditEntriesView } from "@/components/audit-entries";

export default function AuditLogPage() {
  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Audit Log</div>
        <AuditEntriesView />
      </div>
    </ConsoleShell>
  );
}
