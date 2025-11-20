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

// wrap in React.memo to avoid re-rendering unless props change
export default React.memo(ChatMessage, (prevProps, nextProps) => {
  return prevProps.text === nextProps.text && prevProps.sender === nextProps.sender;
});
