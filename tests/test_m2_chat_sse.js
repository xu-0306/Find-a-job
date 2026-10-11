"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");

global.window = global;
const ChatSSE = require(path.join(__dirname, "..", "app", "static", "chat_sse.js"));

function readerFor(chunks) {
  let index = 0;
  const stream = new ReadableStream({
    pull(controller) {
      if (index === chunks.length) controller.close();
      else controller.enqueue(chunks[index++]);
    }
  });
  return stream.getReader();
}

function bytes(text) {
  return new TextEncoder().encode(text);
}

function splitBytes(value, cuts) {
  const result = [];
  let start = 0;
  for (const end of cuts) {
    result.push(value.slice(start, end));
    start = end;
  }
  result.push(value.slice(start));
  return result.filter((chunk) => chunk.length);
}

async function rejects(promise, message) {
  await assert.rejects(promise, (error) => {
    assert(error instanceof Error);
    assert.equal(error.message, message);
    return true;
  });
}

(async () => {
  const unicode = bytes('event: note\r\ndata: {"text":"哈😀"}\r\n\r\n');
  const unicodeChunks = splitBytes(unicode, [1, 2, 10, 24]);
  const unicodeEvents = [];
  const unicodeReader = readerFor(unicodeChunks);
  await ChatSSE.read(unicodeReader, async (event, payload) => unicodeEvents.push([event, payload]));
  assert.deepEqual(unicodeEvents, [["note", {text: "哈😀"}]]);

  const events = [];
  const streamReader = readerFor([
    bytes(": ignored\r"),
    bytes("\ndata: {\"a\":1}\r\n\rdata: {\"b\":2}\r\n\r"),
    bytes("\ndata: {\"c\":3}\n\nevent: later\ndata: {\"d\":4}\n\n")
  ]);
  await ChatSSE.read(streamReader, async (event, payload) => {
    await new Promise((resolve) => setTimeout(resolve, 1));
    events.push([event, payload]);
  });
  assert.deepEqual(events, [["message", {a: 1}], ["message", {b: 2}], ["message", {c: 3}], ["later", {d: 4}]]);

  const multiline = [];
  await ChatSSE.read(readerFor([bytes("event: multi\ndata: {\"a\":\ndata: 1}\n\n")]), (event, payload) => {
    multiline.push([event, payload]);
  });
  assert.deepEqual(multiline, [["multi", {a: 1}]]);

  const unterminated = [];
  await ChatSSE.read(readerFor([bytes("event: ignored\ndata: {\"ignored\":true}")]), (event, payload) => {
    unterminated.push([event, payload]);
  });
  assert.deepEqual(unterminated, []);

  const malformedReader = readerFor([bytes("data: {bad}\n\n")]);
  await rejects(ChatSSE.read(malformedReader, () => {}), "Invalid SSE data");

  const invalidUtf8Reader = readerFor([new Uint8Array([100, 97, 116, 97, 58, 32, 0xc3, 0x28, 10, 10])]);
  await rejects(ChatSSE.read(invalidUtf8Reader, () => {}), "Invalid UTF-8");

  const oversizedReader = readerFor([bytes(`data: ${"x".repeat(1024 * 1024)}\n\n`)]);
  await rejects(ChatSSE.read(oversizedReader, () => {}), "SSE frame too large");

  let released = 0;
  const releaseReader = {
    async read() { return {done: true, value: undefined}; },
    releaseLock() { released += 1; }
  };
  await ChatSSE.read(releaseReader, () => {});
  assert.equal(released, 1);

  console.log("test_m2_chat_sse: 8 checks passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
