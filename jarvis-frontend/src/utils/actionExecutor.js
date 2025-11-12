// src/utils/actionExecutor.js

export async function executeAction(action) {
  if (!action || !action.type) return;

  const type = action.type.toLowerCase();
  const value = action.value || "";

  switch (type) {
    case "open_url":
      const url = normalizeURL(value);
      window.open(url, "_blank");
      speak(`Opening ${url}`);
      break;

    case "search":
      const query = encodeURIComponent(value);
      window.open(`https://www.google.com/search?q=${query}`, "_blank");
      speak(`Searching for ${value}`);
      break;

    case "play_youtube":
      window.open(`https://www.youtube.com/results?search_query=${encodeURIComponent(value)}`, "_blank");
      speak(`Playing ${value} on YouTube.`);
      break;

    case "calculate":
      try {
        const result = eval(value.replace(/[^0-9+\-*/().]/g, ""));
        speak(`The result is ${result}`);
        alert(`Result: ${result}`);
      } catch {
        speak("I couldn't calculate that, sir.");
      }
      break;

    case "fetch_news":
      speak("Getting the latest headlines...");
      window.open("https://news.google.com", "_blank");
      break;

    case "mode_switch":
      speak(`Switching to ${value} mode.`);
      break;

    case "speak":
      speak(value);
      break;

    default:
      console.warn("Unknown action type:", action);
      break;
  }
}

function speak(text) {
  if (!text) return;
  const synth = window.speechSynthesis;
  const utter = new SpeechSynthesisUtterance(text);
  utter.pitch = 1;
  utter.rate = 1.0;
  synth.speak(utter);
}

function normalizeURL(value) {
  if (!value.startsWith("http")) return `https://${value}`;
  return value;
}
