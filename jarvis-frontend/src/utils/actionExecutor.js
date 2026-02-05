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
        const result = safeEvalMath(value);
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

// Safe arithmetic evaluator for simple expressions.
// Supports: + - * / ( ) and decimals. No variables/functions.
function safeEvalMath(input) {
  const raw = String(input || "");
  const expr = raw.replace(/\s+/g, "");
  if (!expr) throw new Error("empty");

  if (!/^[0-9+\-*/().]+$/.test(expr)) throw new Error("invalid_chars");

  const tokens = tokenize(expr);
  const rpn = toRpn(tokens);
  const valueOut = evalRpn(rpn);

  if (!Number.isFinite(valueOut)) throw new Error("not_finite");
  // Avoid long float tails in TTS/UI.
  return Math.round(valueOut * 1e10) / 1e10;
}

function tokenize(expr) {
  const tokens = [];
  let i = 0;
  while (i < expr.length) {
    const ch = expr[i];

    if (ch === "+" || ch === "-" || ch === "*" || ch === "/" || ch === "(" || ch === ")") {
      tokens.push({ type: "op", value: ch });
      i += 1;
      continue;
    }

    // number
    if (ch === "." || (ch >= "0" && ch <= "9")) {
      let j = i;
      let sawDot = false;
      while (j < expr.length) {
        const c = expr[j];
        if (c === ".") {
          if (sawDot) break;
          sawDot = true;
          j += 1;
          continue;
        }
        if (c >= "0" && c <= "9") {
          j += 1;
          continue;
        }
        break;
      }
      const numStr = expr.slice(i, j);
      if (numStr === ".") throw new Error("bad_number");
      tokens.push({ type: "num", value: Number(numStr) });
      i = j;
      continue;
    }

    throw new Error("unexpected");
  }

  return tokens;
}

function toRpn(tokens) {
  const out = [];
  const ops = [];

  const prec = {
    "+": 1,
    "-": 1,
    "*": 2,
    "/": 2,
    "u-": 3,
  };

  function isOp(t, v) {
    return t && t.type === "op" && t.value === v;
  }

  for (let idx = 0; idx < tokens.length; idx += 1) {
    const t = tokens[idx];
    if (t.type === "num") {
      out.push(t);
      continue;
    }

    if (t.type === "op") {
      if (t.value === "(") {
        ops.push(t);
        continue;
      }

      if (t.value === ")") {
        while (ops.length && !isOp(ops[ops.length - 1], "(")) {
          out.push(ops.pop());
        }
        if (!ops.length) throw new Error("mismatched_parens");
        ops.pop(); // pop '('
        continue;
      }

      // unary minus support: start of expression, or preceded by operator or '('
      const prev = idx > 0 ? tokens[idx - 1] : null;
      const unary = t.value === "-" && (!prev || (prev.type === "op" && prev.value !== ")"));
      const opToken = unary ? { type: "op", value: "u-" } : t;

      while (ops.length) {
        const top = ops[ops.length - 1];
        if (!top || top.type !== "op") break;
        if (top.value === "(") break;
        if ((prec[top.value] || 0) >= (prec[opToken.value] || 0)) {
          out.push(ops.pop());
          continue;
        }
        break;
      }

      ops.push(opToken);
      continue;
    }
  }

  while (ops.length) {
    const op = ops.pop();
    if (op.value === "(" || op.value === ")") throw new Error("mismatched_parens");
    out.push(op);
  }

  return out;
}

function evalRpn(rpn) {
  const stack = [];
  for (const t of rpn) {
    if (t.type === "num") {
      stack.push(t.value);
      continue;
    }

    if (t.type === "op") {
      if (t.value === "u-") {
        if (stack.length < 1) throw new Error("bad_expr");
        stack.push(-stack.pop());
        continue;
      }

      if (stack.length < 2) throw new Error("bad_expr");
      const b = stack.pop();
      const a = stack.pop();
      switch (t.value) {
        case "+":
          stack.push(a + b);
          break;
        case "-":
          stack.push(a - b);
          break;
        case "*":
          stack.push(a * b);
          break;
        case "/":
          stack.push(a / b);
          break;
        default:
          throw new Error("bad_op");
      }
      continue;
    }

    throw new Error("bad_token");
  }

  if (stack.length !== 1) throw new Error("bad_expr");
  return stack[0];
}
