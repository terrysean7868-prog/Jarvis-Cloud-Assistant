// src/components/ChatMessage.jsx
import React from "react";
import "../styles/App.css";

const ChatMessage = ({ text, sender }) => {
  return (
    <div className={`chat-message ${sender}`}>
      <div className="bubble">{text}</div>
    </div>
  );
};

export default ChatMessage;
