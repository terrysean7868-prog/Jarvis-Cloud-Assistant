// src/components/PermissionModal.jsx
import React from "react";
import "./PermissionModal.css";

export default function PermissionModal({ title, message, details, allowLabel = "Allow", denyLabel = "Deny", onAllow, onDeny }) {
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

        <div className="perm-actions">
          <button className="perm-button perm-deny" onClick={onDeny}>{denyLabel}</button>
          <button className="perm-button perm-allow" onClick={onAllow}>{allowLabel}</button>
        </div>
      </div>
    </div>
  );
}
