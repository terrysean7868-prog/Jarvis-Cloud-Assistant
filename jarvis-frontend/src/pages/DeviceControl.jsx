import React, { useCallback, useEffect, useState } from "react";
import { dispatchDeviceActions, getDeviceList } from "../utils/api";
import "./autonomyPanel.css";

export default function DeviceControl({ sessionId }) {
  const [devices, setDevices] = useState([]);
  const [deviceId, setDeviceId] = useState("");
  const [command, setCommand] = useState("open_notepad");
  const [result, setResult] = useState("");

  const refresh = useCallback(async () => {
    try {
      const res = await getDeviceList(sessionId);
      const rows = Array.isArray(res?.devices) ? res.devices : [];
      setDevices(rows);
      if (!deviceId && rows.length) {
        const first = String(rows[0]?.device_id || "");
        if (first) setDeviceId(first);
      }
    } catch (e) {
      setResult(`Device list failed: ${e?.message || e}`);
    }
  }, [deviceId, sessionId]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 8000);
    return () => clearInterval(id);
  }, [refresh]);

  const sendAction = async () => {
    if (!deviceId) {
      setResult("Select a device first.");
      return;
    }
    const actionType = String(command || "").trim() || "open_app";
    const action = actionType === "open_notepad"
      ? { type: "open_app", app_name: "notepad" }
      : { type: "execute_command", command: actionType };

    try {
      const res = await dispatchDeviceActions([action], sessionId, `Device control command: ${actionType}`, deviceId);
      setResult(JSON.stringify(res, null, 2));
      await refresh();
    } catch (e) {
      setResult(`Dispatch failed: ${e?.message || e}`);
    }
  };

  return (
    <div className="panel-grid">
      <div className="panel-card">
        <h3 className="panel-title">Connected Devices</h3>
        <div className="panel-list">
          {devices.map((d, idx) => (
            <div className="panel-item" key={String(d?.device_id || idx)}>
              <h4>{String(d?.device_id || "device")}</h4>
              <p>Status: {String(d?.connected ? "connected" : "offline")}</p>
              <p>Capabilities: {JSON.stringify(d?.capabilities || {})}</p>
            </div>
          ))}
          {!devices.length && <div className="panel-item"><p>No connected devices.</p></div>}
        </div>
      </div>
      <div className="panel-card">
        <h3 className="panel-title">Send Device Action</h3>
        <div className="panel-row">
          <select value={deviceId} onChange={(e) => setDeviceId(e.target.value)}>
            <option value="">Select device</option>
            {devices.map((d, idx) => <option key={idx} value={String(d?.device_id || "")}>{String(d?.device_id || "device")}</option>)}
          </select>
        </div>
        <div className="panel-row">
          <input value={command} onChange={(e) => setCommand(e.target.value)} placeholder="open_notepad or command" />
          <button className="panel-btn" onClick={sendAction}>Send</button>
        </div>
        <div className="log-box" style={{ marginTop: 10 }}>{result || "Action response will appear here."}</div>
      </div>
    </div>
  );
}
