"use client";

/**
 * Identity & Access (Phase 2 FE-10, admin UI doc section 14 rendered
 * read-only per ADR-8): SSO config is env-derived this phase - every field
 * displays but nothing is editable, and the banner says exactly how to
 * change it. The client secret is shown strictly as configured-or-not; the
 * value never appears in any backend response. No SCIM UI this phase.
 *
 * Test Connection runs the live discovery fetch the real login flow uses
 * and renders the backend's structured outcome
 * (ok / unreachable / invalid_response / not_configured).
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { Badge, useToast, type BadgeTone } from "@/components/ui";
import { SecretRevealModal } from "@/components/personal-keys";
import {
  ApiError,
  getScimConfig,
  getSsoConfig,
  putScimConfig,
  rotateScimToken,
  testSsoConnection,
  type ScimConfigResponse,
  type SsoConfigResponse,
  type SsoTestConnectionResponse,
} from "@/lib/api";

const TEST_TONES: Record<SsoTestConnectionResponse["status"], { tone: BadgeTone; label: string }> = {
  ok: { tone: "green", label: "Connection OK" },
  unreachable: { tone: "red", label: "Issuer unreachable" },
  invalid_response: { tone: "amber", label: "Invalid issuer response" },
  not_configured: { tone: "gray", label: "Not configured" },
};

function ReadOnlyField({ label, value }: { label: string; value: string }) {
  return (
    <div className="field">
      <label>{label}</label>
      <input type="text" value={value} readOnly disabled />
    </div>
  );
}

// --- SCIM (Phase 3, BD-24, design doc sections 6.2/9.5) ------------------------

function ScimSection() {
  const toast = useToast();
  const [config, setConfig] = useState<ScimConfigResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toggling, setToggling] = useState(false);
  const [rotating, setRotating] = useState(false);
  const [reveal, setReveal] = useState<{ token: string } | null>(null);
  const [copied, setCopied] = useState(false);

  function refresh() {
    getScimConfig()
      .then(setConfig)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load SCIM configuration."));
  }

  useEffect(refresh, []);

  async function handleToggle() {
    if (!config) return;
    setToggling(true);
    try {
      const result = await putScimConfig(!config.enabled);
      setConfig(result);
      toast.push("success", result.enabled ? "SCIM enabled." : "SCIM disabled.");
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to update SCIM.");
    } finally {
      setToggling(false);
    }
  }

  async function handleRotate() {
    setRotating(true);
    try {
      const result = await rotateScimToken();
      setReveal({ token: result.token });
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to rotate the SCIM token.");
    } finally {
      setRotating(false);
    }
  }

  function copyBaseUrl() {
    if (!config) return;
    navigator.clipboard?.writeText(config.base_url);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  if (!config) {
    return error ? <div className="banner banner-error">{error}</div> : <div className="skeleton skeleton-text" style={{ width: "40%" }} />;
  }

  return (
    <div className="panel">
      <div className="page-header-row">
        <div className="panel-title">SCIM 2.0 provisioning</div>
        <Badge tone={config.enabled ? "green" : "gray"}>{config.enabled ? "Enabled" : "Disabled"}</Badge>
      </div>
      <p className="text-muted">
        Lets an IdP provision/deprovision users and push group (team) membership. Off by default
        - a SCIM push can never grant an org-wide role, that stays an Org Admin action.
      </p>
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          id="scim-enabled"
          checked={config.enabled}
          onChange={handleToggle}
          disabled={toggling}
          style={{ width: "auto" }}
        />
        <label htmlFor="scim-enabled" style={{ margin: 0 }}>
          Enable SCIM provisioning
        </label>
      </div>
      <div className="field">
        <label>SCIM base URL</label>
        <div className="secret-box">
          <span style={{ flex: 1, wordBreak: "break-all" }} className="mono">
            {config.base_url}
          </span>
          <button className="btn" onClick={copyBaseUrl}>
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
        <div className="field-hint">Paste this as the SCIM base URL in your IdP&apos;s SCIM configuration.</div>
      </div>
      <div className="field">
        <label>Bearer token</label>
        <div>
          {config.token_created_at ? (
            <span className="text-muted">
              Issued {new Date(config.token_created_at).toLocaleString()} (never shown again)
            </span>
          ) : (
            <span className="text-muted">No token issued yet.</span>
          )}
        </div>
      </div>
      <div className="modal-actions">
        <button className="btn btn-primary" onClick={handleRotate} disabled={rotating}>
          {rotating ? "Rotating..." : config.token_created_at ? "Rotate token" : "Generate token"}
        </button>
      </div>
      <div className="field-hint">
        Rotating immediately invalidates the previous token - no overlap window, unlike scheduled
        key rotation. Update your IdP&apos;s SCIM config with the new token right away.
      </div>

      {reveal ? (
        <SecretRevealModal
          title="Save this SCIM bearer token now"
          secret={reveal.token}
          onDone={() => setReveal(null)}
        />
      ) : null}
    </div>
  );
}

export default function IdentityPage() {
  const [config, setConfig] = useState<SsoConfigResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<SsoTestConnectionResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    getSsoConfig()
      .then((data) => {
        if (!cancelled) setConfig(data);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Failed to load SSO configuration.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleTest() {
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(await testSsoConnection());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Test connection failed.");
    } finally {
      setTesting(false);
    }
  }

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Identity &amp; Access</div>

        <div className="banner banner-info">
          Configured via environment variables (GATEKEY_OIDC_*) - edit .env and restart
          the gateway to change these values.
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}

        {config ? (
          <div className="panel">
            <div className="page-header-row">
              <div className="panel-title">SSO (OIDC)</div>
              <Badge tone={config.enabled ? "green" : "gray"}>
                {config.enabled ? "Enabled" : "Not configured"}
              </Badge>
            </div>
            <ReadOnlyField label="Issuer URL" value={config.issuer_url ?? "(not set)"} />
            <ReadOnlyField label="Client ID" value={config.client_id ?? "(not set)"} />
            <ReadOnlyField label="Redirect URI" value={config.redirect_uri ?? "(not set)"} />
            <div className="field">
              <label>Client secret</label>
              <div>
                {config.client_secret.configured ? (
                  <Badge tone="green">Configured (never shown)</Badge>
                ) : (
                  <Badge tone="gray">Not configured</Badge>
                )}
              </div>
            </div>
            <div className="modal-actions">
              <button className="btn btn-primary" onClick={handleTest} disabled={testing}>
                {testing ? "Testing..." : "Test connection"}
              </button>
            </div>
            {testResult ? (
              <p>
                <Badge tone={TEST_TONES[testResult.status].tone}>
                  {TEST_TONES[testResult.status].label}
                </Badge>{" "}
                <span className="text-muted">{testResult.detail}</span>
              </p>
            ) : null}
          </div>
        ) : error ? null : (
          <div className="skeleton skeleton-text" style={{ width: "40%" }} />
        )}

        <ScimSection />
      </div>
    </ConsoleShell>
  );
}
