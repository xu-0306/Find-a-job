"use strict";

(function (root) {
  const STATES = Object.freeze(["idle", "thinking", "happy", "mail", "encourage"]);
  const TRANSIENT_STATES = new Set(["happy", "mail", "encourage"]);
  const PRIORITY = Object.freeze({idle: 0, thinking: 1, happy: 2, mail: 3, encourage: 4});
  const MAX_DISPLAY_SIZE = 128;

  function validMetadata(value) {
    return value && STATES.includes(value.state)
      && Number.isInteger(value.frame_width) && value.frame_width >= 1 && value.frame_width <= 512
      && Number.isInteger(value.frame_height) && value.frame_height >= 1 && value.frame_height <= 512
      && Number.isInteger(value.frame_count) && value.frame_count >= 1 && value.frame_count <= 120
      && Number.isInteger(value.fps) && value.fps >= 1 && value.fps <= 30;
  }

  function quoteUrl(value) {
    return `url("${String(value).replace(/[\\"]/g, "\\$&")}")`;
  }

  function pixels(value) {
    return `${Number(value.toFixed(4))}px`;
  }

  function reducedValue(value) {
    if (typeof value === "boolean") return value;
    return Boolean(value && value.matches);
  }

  function paint(element, value, index, maxSize = MAX_DISPLAY_SIZE, fallbackUrl = "/static/companion.png") {
    const width = value ? value.frame_width : 64;
    const height = value ? value.frame_height : 64;
    const scale = Math.min(1, maxSize / Math.max(width, height));
    const frameWidth = width * scale;
    const frameHeight = height * scale;
    const frameIndex = value ? Math.max(0, Math.min(index, value.frame_count - 1)) : 0;
    element.style.backgroundImage = quoteUrl(value ? `/api/sprites/${value.state}/image` : fallbackUrl);
    element.style.backgroundPosition = `${pixels(-frameIndex * frameWidth)} 0px`;
    element.style.backgroundSize = `${pixels(frameWidth * (value ? value.frame_count : 1))} ${pixels(frameHeight)}`;
    element.style.backgroundRepeat = "no-repeat";
    element.style.width = pixels(frameWidth);
    element.style.height = pixels(frameHeight);
  }

  function renderStatic(element, sprites, state = "idle", maxSize = 48) {
    if (!element || !element.style) return;
    const metadata = (Array.isArray(sprites) ? sprites : []).filter(validMetadata);
    const value = metadata.find(item => item.state === state) || metadata.find(item => item.state === "idle") || metadata[0];
    paint(element, value, 0, maxSize);
  }

  function create(element, sprites, options = {}) {
    if (!element || !element.style) throw new TypeError("CompanionSprite requires an element");
    const settings = options && typeof options === "object" ? options : {};
    const fallbackUrl = typeof settings.fallbackUrl === "string" && settings.fallbackUrl
      ? settings.fallbackUrl : "/static/companion.png";
    const metadata = Array.isArray(sprites) ? sprites.slice() : [];
    const maxSize = Number.isFinite(settings.maxDisplaySize) && settings.maxDisplaySize > 0
      ? settings.maxDisplaySize : MAX_DISPLAY_SIZE;
    const media = settings.reducedMotion === undefined
      ? (typeof root.matchMedia === "function" ? root.matchMedia("(prefers-reduced-motion: reduce)") : null)
      : (settings.reducedMotion && typeof settings.reducedMotion === "object" ? settings.reducedMotion : null);
    let reducedMotion = reducedValue(settings.reducedMotion === undefined ? media : settings.reducedMotion);
    let paused = Boolean(settings.paused);
    let destroyed = false;
    let thinking = false;
    let active = null;
    let frame = 0;
    let frameTimer = null;
    let transitionTimer = null;

    const defaultSetTimeout = typeof root.setTimeout === "function" ? root.setTimeout.bind(root) : setTimeout;
    const defaultClearTimeout = typeof root.clearTimeout === "function" ? root.clearTimeout.bind(root) : clearTimeout;
    const setTimer = typeof settings.setTimeout === "function" ? settings.setTimeout : defaultSetTimeout;
    const clearTimer = typeof settings.clearTimeout === "function" ? settings.clearTimeout : defaultClearTimeout;

    function find(state) {
      return metadata.find((item) => validMetadata(item) && item.state === state) || null;
    }

    function visualMetadata(state) {
      return find(state) || find("idle") || metadata.find(validMetadata) || null;
    }

    function currentState() {
      return active ? active.state : (thinking ? "thinking" : "idle");
    }

    function clearFrameTimer() {
      if (frameTimer !== null) {
        clearTimer(frameTimer);
        frameTimer = null;
      }
    }

    function clearTransitionTimer() {
      if (transitionTimer !== null) {
        clearTimer(transitionTimer);
        transitionTimer = null;
      }
    }

    function apply(state, index, value = visualMetadata(state)) {
      if (typeof settings.renderFrame === "function") settings.renderFrame(value, index);
      else paint(element, value, index, maxSize, fallbackUrl);
    }

    function scheduleFrame(state, value) {
      if (destroyed || paused || reducedMotion || !value || value.frame_count <= 1) return;
      frameTimer = setTimer(() => {
        frameTimer = null;
        if (destroyed || currentState() !== state) return;
        const current = visualMetadata(state);
        if (!current) return;
        frame = (frame + 1) % current.frame_count;
        apply(state, frame, current);
        scheduleFrame(state, current);
      }, state === "idle" && value.state === "idle" && frame === 0 ? 2000 : 1000 / value.fps);
    }

    function render(state) {
      clearFrameTimer();
      frame = 0;
      const value = visualMetadata(state);
      apply(state, frame, value);
      scheduleFrame(state, value);
    }

    function mediaChanged(event) {
      if (destroyed) return;
      reducedMotion = reducedValue(event && typeof event.matches === "boolean" ? event : media);
      render(currentState());
    }

    let removeMediaListener = null;
    if (media && typeof media.addEventListener === "function") {
      media.addEventListener("change", mediaChanged);
      if (typeof media.removeEventListener === "function") {
        removeMediaListener = () => media.removeEventListener("change", mediaChanged);
      }
    } else if (media && typeof media.addListener === "function") {
      media.addListener(mediaChanged);
      if (typeof media.removeListener === "function") {
        removeMediaListener = () => media.removeListener(mediaChanged);
      }
    }

    const controller = {
      setPaused(value) {
        if (destroyed) return;
        paused = Boolean(value);
        clearFrameTimer();
        if (!paused) scheduleFrame(currentState(), visualMetadata(currentState()));
      },

      setThinking(value) {
        if (destroyed) return;
        thinking = Boolean(value);
        if (!active) render(currentState());
      },

      play(state, durationMs = 3000) {
        if (destroyed || !TRANSIENT_STATES.has(state)) return;
        if (active && PRIORITY[state] < PRIORITY[active.state]) return;
        const duration = typeof durationMs === "number" && Number.isFinite(durationMs)
          ? Math.max(0, durationMs) : 3000;
        clearTransitionTimer();
        active = {state, token: {}};
        const token = active.token;
        render(state);
        transitionTimer = setTimer(() => {
          transitionTimer = null;
          if (destroyed || !active || active.token !== token) return;
          active = null;
          render(currentState());
        }, duration);
      },

      reset() {
        if (destroyed) return;
        clearFrameTimer();
        clearTransitionTimer();
        active = null;
        thinking = false;
        render("idle");
      },

      destroy() {
        if (destroyed) return;
        destroyed = true;
        clearFrameTimer();
        clearTransitionTimer();
        active = null;
        if (removeMediaListener) removeMediaListener();
        removeMediaListener = null;
      },
    };

    render("idle");
    return controller;
  }

  const api = {create, renderStatic};
  root.CompanionSprite = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window === "object" ? window : globalThis);
