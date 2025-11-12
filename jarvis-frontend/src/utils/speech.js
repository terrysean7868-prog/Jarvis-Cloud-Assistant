// src/utils/speech.js
export function speak(text, onEnd) {
  const synth = window.speechSynthesis;
  const utter = new SpeechSynthesisUtterance(text);
  utter.pitch = 1.1;
  utter.rate = 1.05;
  utter.onend = onEnd || (() => {});
  synth.speak(utter);
}

export async function listenOnce() {
  return new Promise((resolve) => {
    const SpeechRecognition =
      window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      alert("Speech recognition not supported in this browser.");
      return resolve(null);
    }

    const recognition = new SpeechRecognition();
    recognition.lang = "en-US";
    recognition.interimResults = false;
    recognition.onresult = (e) => resolve(e.results[0][0].transcript);
    recognition.onerror = () => resolve(null);
    recognition.onend = () => {};
    recognition.start();
  });
}
