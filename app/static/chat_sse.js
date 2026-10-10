"use strict";

(function (root) {
  const MAX_BYTES = 1024 * 1024;

  function codePointBytes(character) {
    const codePoint = character.codePointAt(0);
    if (codePoint < 0x80) return 1;
    if (codePoint < 0x800) return 2;
    if (codePoint < 0x10000) return 3;
    return 4;
  }

  async function read(reader, onEvent) {
    const decoder = new TextDecoder("utf-8", {fatal: true});
    let line = "";
    let lineBytes = 0;
    let skipLf = false;
    let frameBytes = 0;
    let eventName = "";
    let dataLines = [];

    function addLine(value) {
      frameBytes += lineBytes;
      if (frameBytes > MAX_BYTES) throw new Error("SSE frame too large");

      if (value.startsWith(":")) return;
      const separator = value.indexOf(":");
      const field = separator === -1 ? value : value.slice(0, separator);
      let fieldValue = separator === -1 ? "" : value.slice(separator + 1);
      if (fieldValue.startsWith(" ")) fieldValue = fieldValue.slice(1);
      if (field === "event") eventName = fieldValue;
      else if (field === "data") dataLines.push(fieldValue);
    }

    async function dispatch() {
      if (!dataLines.length) {
        eventName = "";
        frameBytes = 0;
        return;
      }
      const payloadText = dataLines.join("\n");
      dataLines = [];
      const name = eventName || "message";
      eventName = "";
      frameBytes = 0;
      let payload;
      try {
        payload = JSON.parse(payloadText);
      } catch (_) {
        throw new Error("Invalid SSE data");
      }
      await onEvent(name, payload);
    }

    async function consume(text) {
      for (const character of text) {
        if (skipLf) {
          skipLf = false;
          if (character === "\n") continue;
        }
        if (character === "\r" || character === "\n") {
          const blank = line.length === 0;
          addLine(line);
          line = "";
          lineBytes = 0;
          if (character === "\r") skipLf = true;
          if (blank) await dispatch();
          continue;
        }
        line += character;
        lineBytes += codePointBytes(character);
        if (lineBytes > MAX_BYTES) throw new Error("SSE frame too large");
      }
    }

    try {
      while (true) {
        const result = await reader.read();
        if (result.done) {
          let text;
          try {
            text = decoder.decode();
          } catch (_) {
            throw new Error("Invalid UTF-8");
          }
          await consume(text);
          break;
        }
        let text;
        try {
          text = decoder.decode(result.value, {stream: true});
        } catch (_) {
          throw new Error("Invalid UTF-8");
        }
        await consume(text);
      }
    } finally {
      reader.releaseLock();
    }
  }

  root.ChatSSE = {read};
  if (typeof module !== "undefined" && module.exports) module.exports = root.ChatSSE;
})(typeof window === "object" ? window : globalThis);
