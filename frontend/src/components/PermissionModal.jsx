// src/components/PermissionModal.jsx
import React, { useCallback, useMemo, useState } from "react";
import "./PermissionModal.css";

export default function PermissionModal({ title, message, details, copyFields, allowLabel = "Allow", denyLabel = "Deny", onAllow, onDeny }) {
  const fields = useMemo(() => {
    if (!Array.isArray(copyFields)) return [];
    return copyFields
      .filter((f) => f && typeof f === "object")
      .map((f) => ({
        label: String(f.label || "").trim(),
        value: String(f.value || "").trim(),
      }))
      .filter((f) => f.label && f.value);
  }, [copyFields]);

  const [copiedLabel, setCopiedLabel] = useState(null);

  const copyValue = useCallback(async (label, value) => {
    const v = String(value || "");
    if (!v) return;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(v);
        setCopiedLabel(label);
        setTimeout(() => setCopiedLabel(null), 900);
        return;
      }
    } catch {
      // fallthrough
    }
    try {
      window.prompt(`Copy ${label}:`, v);
    } catch {
      // ignore
    }
  }, []);

  return (
    <div className="perm-modal-overlay" onClick={onDeny}>
      <div className="perm-modal" onClick={(e) => e.stopPropagation()}>
        <button className="perm-close" onClick={onDeny}>×</button>

        <div className="perm-header">
          <h2>{title || "Permission required"}</h2>
          <p>{message || "This action needs permission on your PC agent."}</p>
        </div>

        {details ? (
          <div className="perm-details">
            {details}
          </div>
        ) : null}

        {fields.length ? (
          <div className="perm-copy-fields">
            {fields.map((f) => (
              <div key={f.label} className="perm-copy-row">
                <div className="perm-copy-label">{f.label}</div>
                <div className="perm-copy-controls">
                  <input className="perm-copy-input" value={f.value} readOnly onFocus={(e) => e.target.select()} />
                  <button
                    className="perm-button perm-copy"
                    onClick={() => copyValue(f.label, f.value)}
                    type="button"
                  >
                    {copiedLabel === f.label ? "Copied" : "Copy"}
                  </button>
                </div>
              </div>
            ))}
          </div>
        ) : null}

        <div className="perm-actions">
          <button className="perm-button perm-deny" onClick={onDeny}>{denyLabel}</button>
          <button className="perm-button perm-allow" onClick={onAllow}>{allowLabel}</button>
        </div>
      </div>
    </div>
  );
}
