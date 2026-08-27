"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const MAX_SEED = (1n << 64n) - 1n;
const state = {
  config: null,
  mode: "t2v",
  health: null,
  uploading: 0,
  assets: {
    first_frame: null,
    last_frame: null,
    ref_images: [],
    ref_videos: [],
    ref_audios: [],
  },
  jobs: [],
  activeJobId: null,
  activeJob: null,
  jobTimer: null,
};

const elements = {
  themeSelect: $("#themeSelect"),
  themeStatus: $("#themeStatus"),
  form: $("#renderForm"),
  modeTabs: $("#modeTabs"),
  modeDescription: $("#modeDescription"),
  prompt: $("#prompt"),
  promptCount: $("#promptCount"),
  profile: $("#profile"),
  profileDescription: $("#profileDescription"),
  resolution: $("#resolution"),
  customDimensions: $("#customDimensions"),
  width: $("#width"),
  height: $("#height"),
  duration: $("#duration"),
  durationLabel: $("#durationLabel"),
  frameCount: $("#frameCount"),
  actualDuration: $("#actualDuration"),
  seed: $("#seed"),
  refImageSize: $("#refImageSize"),
  identityField: $("#identityField"),
  i2vAssets: $("#i2vAssets"),
  r2vAssets: $("#r2vAssets"),
  referenceList: $("#referenceList"),
  tagMap: $("#tagMap"),
  tagChips: $("#tagChips"),
  formStatus: $("#formStatus"),
  renderButton: $("#renderButton"),
  engineStatus: $("#engineStatus"),
  engineCallout: $("#engineCallout"),
  engineCalloutTitle: $("#engineCalloutTitle"),
  engineCommand: $("#engineCommand"),
  metricEngine: $("#metricEngine"),
  metricEngineNote: $("#metricEngineNote"),
  metricGpu: $("#metricGpu"),
  metricGpuNote: $("#metricGpuNote"),
  metricModels: $("#metricModels"),
  metricModelsNote: $("#metricModelsNote"),
  lastChecked: $("#lastChecked"),
  jobList: $("#jobList"),
  emptyStage: $("#emptyStage"),
  renderProgress: $("#renderProgress"),
  progressTitle: $("#progressTitle"),
  progressDetail: $("#progressDetail"),
  progressBar: $("#progressBar"),
  progressPercent: $("#progressPercent"),
  progressTime: $("#progressTime"),
  outputVideo: $("#outputVideo"),
  viewerActions: $("#viewerActions"),
  downloadOutput: $("#downloadOutput"),
  outputMeta: $("#outputMeta"),
};

const recipes = {
  t2v: `A cinematic single-take scene of [subject] in [environment].
SHOT: Begin with [opening composition]. The camera [camera movement] while [subject action]. Lighting shifts from [light A] to [light B], revealing [important detail]. End on [final composition].
Audio: [ambience], [specific sound effects], and [music direction]. Dialogue: “[line, if any].” No subtitles or on-screen text.`,
  i2v: `Continue naturally from the supplied first frame. Preserve the subject, materials, lighting, and environment.
SHOT 1: The camera [movement] as [subject action].
SHOT 2: [transition] into [new framing/action].
ENDING: Settle into the supplied last frame if present; otherwise end on [final composition].
Audio: [ambience], [tactile effects], and [music/dialogue]. No subtitles or on-screen text.`,
  r2v: `Use <Picture 1> as the primary identity and visual reference. Preserve its defining features.
SHOT 1: [subject and action], framed as [composition], with [camera movement].
SHOT 2: [continuation or cut], retaining the reference identity and style.
Audio: Use <Audio 1> for voice or sound character when supplied; add [ambience/effects/music]. No subtitles or on-screen text.`,
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    credentials: "same-origin",
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(options.headers || {}),
    },
  });
  const contentType = response.headers.get("content-type") || "";
  let body;
  if (contentType.includes("json")) {
    body = await response.json();
  } else {
    body = { error: (await response.text()).slice(0, 1000) || `Request failed (${response.status})` };
  }
  if (!response.ok) {
    const detail = body?.error;
    const message = typeof detail === "string" ? detail : detail?.message || `Request failed (${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    error.details = body?.details || detail;
    throw error;
  }
  return body;
}

function setFormMessage(message = "", kind = "") {
  elements.formStatus.textContent = message;
  elements.formStatus.className = `form-status${kind ? ` ${kind}` : ""}`;
}

function selectedProfile() {
  return state.config?.profiles.find((profile) => profile.id === elements.profile.value) || null;
}

function deviceCount() {
  const devices = state.health?.stats?.devices;
  return Array.isArray(devices) ? devices.length : 0;
}

function alignedFrames(seconds) {
  const frames = Math.max(5, Math.round(seconds * 24));
  return frames + ((5 - (frames % 17)) + 17) % 17;
}

function updateDuration() {
  const profile = selectedProfile();
  const minimum = profile?.turbo ? 2 : 5;
  elements.duration.min = String(minimum);
  if (Number(elements.duration.value) < minimum) elements.duration.value = String(minimum);
  const seconds = Number(elements.duration.value);
  const frames = alignedFrames(seconds);
  elements.durationLabel.textContent = `${seconds.toFixed(1)} s`;
  elements.frameCount.textContent = `${frames} frames`;
  elements.actualDuration.textContent = `actual ${(frames / 24).toFixed(2)} s`;
}

function updateRenderButtonCopy() {
  const profile = selectedProfile();
  if (!profile) return;
  const stepCount = state.mode === "r2v" ? profile.steps_ref : profile.steps_fl;
  $("span", elements.renderButton).textContent = "Create H3 video";
  $("small", elements.renderButton).textContent = `${profile.precision.toUpperCase()} · ${stepCount} steps · ${profile.dual_gpu ? "dual stage" : "offload"}`;
}

function setupConfig(config) {
  state.config = config;
  elements.engineCommand.textContent = config.engine_start_command || "./scripts/start_comfyui.sh";

  elements.modeTabs.replaceChildren();
  for (const mode of config.modes) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "mode-tab";
    button.role = "tab";
    button.dataset.mode = mode.id;
    button.setAttribute("aria-selected", String(mode.id === state.mode));
    button.innerHTML = `${escapeHtml(mode.label)}<span>${escapeHtml(mode.short)}</span>`;
    button.addEventListener("click", () => setMode(mode.id));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const modes = config.modes.map((item) => item.id);
      let index = modes.indexOf(state.mode);
      if (event.key === "Home") index = 0;
      else if (event.key === "End") index = modes.length - 1;
      else index = (index + (event.key === "ArrowRight" ? 1 : -1) + modes.length) % modes.length;
      setMode(modes[index]);
      $(`.mode-tab[data-mode="${modes[index]}"]`, elements.modeTabs)?.focus();
    });
    elements.modeTabs.append(button);
  }

  elements.profile.replaceChildren();
  for (const profile of config.profiles) {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = profile.label;
    option.dataset.dual = String(profile.dual_gpu);
    elements.profile.append(option);
  }
  elements.profile.value = config.defaults.profile;

  elements.resolution.replaceChildren();
  for (const resolution of config.resolutions) {
    const option = document.createElement("option");
    option.value = resolution.id;
    option.textContent = resolution.label;
    option.dataset.width = resolution.width;
    option.dataset.height = resolution.height;
    elements.resolution.append(option);
  }
  const custom = document.createElement("option");
  custom.value = "custom";
  custom.textContent = "Custom · multiples of 32";
  elements.resolution.append(custom);
  elements.resolution.value = config.resolutions.find((item) => item.width === config.defaults.width && item.height === config.defaults.height)?.id || config.resolutions[0].id;
  elements.width.value = String(config.defaults.width);
  elements.height.value = String(config.defaults.height);
  elements.duration.value = String(config.defaults.duration);
  setMode(config.defaults.mode);
  updateProfile();
  updateResolution();
}

function setMode(modeId) {
  if (!state.config?.modes.some((mode) => mode.id === modeId)) return;
  state.mode = modeId;
  for (const button of $$(".mode-tab", elements.modeTabs)) {
    const active = button.dataset.mode === modeId;
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  }
  const mode = state.config.modes.find((item) => item.id === modeId);
  elements.modeDescription.textContent = mode?.description || "";
  elements.i2vAssets.hidden = modeId !== "i2v";
  elements.r2vAssets.hidden = modeId !== "r2v";
  elements.identityField.hidden = modeId !== "r2v";
  elements.prompt.placeholder = {
    t2v: "Describe subject, action, camera, lighting, timing, dialogue, ambience, effects, and music…",
    i2v: "Describe how the supplied frame comes alive, how the camera moves, and what we hear…",
    r2v: "Use tags such as <Picture 1>, <Video 1>, or <Audio 1>, then direct the new scene and sound…",
  }[modeId];
  renderTagMap();
  updateRenderButtonCopy();
  validateForm();
}

function updateProfile() {
  const profile = selectedProfile();
  elements.profileDescription.textContent = profile?.description || "";
  updateDuration();
  updateRenderButtonCopy();
  validateForm();
}

function updateResolution() {
  const option = elements.resolution.selectedOptions[0];
  const custom = elements.resolution.value === "custom";
  elements.customDimensions.hidden = !custom;
  if (!custom && option) {
    elements.width.value = option.dataset.width;
    elements.height.value = option.dataset.height;
  }
  validateForm();
}

function parseSeed() {
  const value = elements.seed.value.trim();
  if (!/^[0-9]+$/.test(value)) throw new Error("Seed must contain digits only.");
  const seed = BigInt(value);
  if (seed > MAX_SEED) throw new Error("Seed is larger than H3 supports.");
  return seed.toString();
}

function validateForm({ announce = false } = {}) {
  if (!state.config) return false;
  let message = "";
  try {
    if (!elements.prompt.value.trim()) throw new Error("Write a scene and sound prompt first.");
    if (state.mode === "i2v" && !state.assets.first_frame) throw new Error("Image-to-video needs a first frame.");
    if (state.mode === "r2v" && ![state.assets.ref_images, state.assets.ref_videos, state.assets.ref_audios].some((list) => list.length)) {
      throw new Error("Reference-to-video needs at least one picture, video, or audio file.");
    }
    const width = Number(elements.width.value);
    const height = Number(elements.height.value);
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 256 || height < 256 || width > 1344 || height > 1344 || width % 32 || height % 32) {
      throw new Error("Canvas dimensions must be multiples of 32 between 256 and 1344.");
    }
    if (width * height > 768 * 1344) throw new Error("Canvas area exceeds H3’s local 768-short-edge limit.");
    parseSeed();
    if (state.uploading) throw new Error(`${state.uploading} upload${state.uploading === 1 ? " is" : "s are"} still processing.`);
    if (!state.health?.connected) throw new Error("Start the verified ComfyUI engine before rendering.");
    if (state.health.ready === false) throw new Error(state.health.message || "The H3 engine is not ready yet.");
    const profile = selectedProfile();
    if (profile?.dual_gpu && deviceCount() < 2) throw new Error("This profile needs both RTX 4090 GPUs.");
  } catch (error) {
    message = error.message;
  }
  elements.renderButton.disabled = Boolean(message);
  if (announce) setFormMessage(message, message ? "error" : "");
  return !message;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "";
  const units = ["B", "KiB", "MiB", "GiB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
}

async function uploadFile(file, kind) {
  const form = new FormData();
  form.append("file", file, file.name);
  state.uploading += 1;
  validateForm();
  try {
    const result = await api(`/api/uploads?kind=${encodeURIComponent(kind)}`, { method: "POST", body: form });
    return { ...result, metadata: result.metadata || {} };
  } finally {
    state.uploading -= 1;
    validateForm();
  }
}

async function handleSingleUpload(input) {
  const file = input.files?.[0];
  if (!file) return;
  const zone = input.closest(".drop-zone");
  zone?.classList.add("uploading");
  setFormMessage(`Uploading ${file.name}…`);
  try {
    const record = await uploadFile(file, input.dataset.upload);
    state.assets[input.dataset.single] = record;
    zone?.classList.add("uploaded");
    $(".asset-name", zone).textContent = `${record.name} · ${formatBytes(record.size)}`;
    setFormMessage(`${record.name} is ready.`, "success");
  } catch (error) {
    input.value = "";
    setFormMessage(error.message, "error");
  } finally {
    zone?.classList.remove("uploading");
    validateForm();
  }
}

async function handleListUpload(input) {
  const files = [...(input.files || [])];
  if (!files.length) return;
  const key = input.dataset.list;
  const limits = { ref_images: 9, ref_videos: 3, ref_audios: 3 };
  const remaining = limits[key] - state.assets[key].length;
  if (remaining <= 0) {
    setFormMessage(`The ${key.replaceAll("_", " ")} limit is ${limits[key]}.`, "error");
    input.value = "";
    return;
  }
  const accepted = files.slice(0, remaining);
  const zone = input.closest(".compact-drop");
  zone?.classList.add("uploading");
  for (const file of accepted) {
    setFormMessage(`Uploading ${file.name}…`);
    try {
      const record = await uploadFile(file, input.dataset.upload);
      if (key === "ref_videos") record.soundtrack = null;
      state.assets[key].push(record);
      renderReferences();
      setFormMessage(`${record.name} is ready.`, "success");
    } catch (error) {
      setFormMessage(error.message, "error");
      break;
    }
  }
  zone?.classList.remove("uploading");
  input.value = "";
  validateForm();
}

async function attachSoundtrack(index) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "audio/*,.flac,.m4a,.opus";
  input.addEventListener("change", async () => {
    const file = input.files?.[0];
    if (!file || !state.assets.ref_videos[index]) return;
    setFormMessage(`Uploading soundtrack ${file.name}…`);
    try {
      const record = await uploadFile(file, "audio");
      state.assets.ref_videos[index].soundtrack = record;
      renderReferences();
      setFormMessage(`Soundtrack attached to ${state.assets.ref_videos[index].name}.`, "success");
    } catch (error) {
      setFormMessage(error.message, "error");
    }
  }, { once: true });
  input.click();
}

function removeAsset(key, index = null) {
  if (index === null) state.assets[key] = null;
  else state.assets[key].splice(index, 1);
  renderReferences();
  validateForm();
}

function renderReferences() {
  elements.referenceList.replaceChildren();
  const groups = [
    ["ref_images", "pic", "Picture"],
    ["ref_videos", "vid", "Video"],
    ["ref_audios", "aud", "Audio"],
  ];
  for (const [key, short, label] of groups) {
    state.assets[key].forEach((asset, index) => {
      const card = $("#assetTemplate").content.firstElementChild.cloneNode(true);
      $(".asset-kind", card).textContent = short;
      $(".asset-copy strong", card).textContent = asset.name;
      $(".asset-copy small", card).textContent = `${label} ${index + 1} · ${formatBytes(asset.size)}`;
      $(".remove-asset", card).addEventListener("click", () => removeAsset(key, index));
      if (key === "ref_videos") {
        const attach = document.createElement("button");
        attach.type = "button";
        attach.className = "text-button";
        attach.textContent = asset.soundtrack ? `Soundtrack: ${asset.soundtrack.name}` : "Attach separate soundtrack";
        attach.addEventListener("click", () => attachSoundtrack(index));
        $(".asset-copy", card).append(attach);
      }
      elements.referenceList.append(card);
    });
  }
  renderTagMap();
}

function renderTagMap() {
  const tags = [];
  state.assets.ref_images.forEach((_, index) => tags.push(`<Picture ${index + 1}>`));
  let audioIndex = 1;
  state.assets.ref_videos.forEach((video, index) => {
    if (video.soundtrack || video.metadata?.has_audio) tags.push(`<Audio ${audioIndex++}>`);
    tags.push(`<Video ${index + 1}>`);
  });
  state.assets.ref_audios.forEach(() => tags.push(`<Audio ${audioIndex++}>`));
  elements.tagMap.hidden = state.mode !== "r2v" || !tags.length;
  elements.tagChips.replaceChildren();
  for (const tag of tags) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tag-chip";
    button.textContent = tag;
    button.addEventListener("click", () => insertAtCursor(elements.prompt, `${tag} `));
    elements.tagChips.append(button);
  }
}

function insertAtCursor(textarea, value) {
  const start = textarea.selectionStart ?? textarea.value.length;
  const end = textarea.selectionEnd ?? start;
  textarea.setRangeText(value, start, end, "end");
  textarea.focus();
  updatePromptCount();
  validateForm();
}

function updatePromptCount() {
  elements.promptCount.textContent = `${elements.prompt.value.length.toLocaleString()} / 12,000`;
}

function clearReferences() {
  state.assets.ref_images = [];
  state.assets.ref_videos = [];
  state.assets.ref_audios = [];
  $$('input[data-list]').forEach((input) => { input.value = ""; });
  renderReferences();
  setFormMessage("Reference handles cleared from this browser session.");
}

function clearKeyframes() {
  state.assets.first_frame = null;
  state.assets.last_frame = null;
  $$('#i2vAssets input[data-single]').forEach((input) => { input.value = ""; });
  $$("#i2vAssets .drop-zone").forEach((zone) => {
    zone.classList.remove("uploaded", "uploading");
    const name = $(".asset-name", zone);
    if (name) name.textContent = "";
  });
  setFormMessage("Keyframe handles cleared from this browser session.");
  validateForm();
}

function randomSeed() {
  const values = new Uint32Array(2);
  crypto.getRandomValues(values);
  elements.seed.value = ((BigInt(values[0]) << 32n) | BigInt(values[1])).toString();
  validateForm();
}

async function refreshHealth() {
  try {
    state.health = await api("/api/health?deep=1");
  } catch (error) {
    state.health = { connected: false, ready: false, message: error.message };
  }
  renderHealth();
  validateForm();
}

function renderHealth() {
  const health = state.health || {};
  const connected = health.connected === true;
  const ready = connected && health.ready !== false;
  elements.engineStatus.className = `status-pill ${ready ? "ready" : connected ? "warn" : ""}`;
  $("span:last-child", elements.engineStatus).textContent = ready ? "H3 engine ready" : connected ? "Engine needs attention" : "Engine offline";
  elements.engineCallout.hidden = ready;
  elements.engineCalloutTitle.textContent = connected ? "H3 runtime is not ready" : "Generation engine is offline";
  elements.metricEngine.textContent = ready ? "Ready" : connected ? "Connected" : "Offline";
  elements.metricEngineNote.textContent = connected ? "ComfyUI · loopback" : "127.0.0.1:8188";

  const count = deviceCount();
  elements.metricGpu.textContent = connected ? `${count} × RTX 4090` : "—";
  elements.metricGpuNote.textContent = count >= 2 ? "DiT · encoder/VAE split" : count === 1 ? "Single-GPU offload only" : "Waiting for engine";

  const modelState = health.model_status || (ready ? "verified" : connected ? "checking" : "unknown");
  elements.metricModels.textContent = modelState === "verified" ? "Verified" : modelState === "downloading" ? "Downloading" : modelState === "invalid" ? "Blocked" : "Checking";
  elements.metricModelsNote.textContent = health.model_note || (ready ? "Aligned nine-file bundle" : "Aligned H3 bundle");
  elements.lastChecked.textContent = `Checked ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;

  for (const option of [...elements.profile.options]) {
    if (option.dataset.dual === "true") option.disabled = connected && count < 2;
  }
}

function renderPayload() {
  const payload = {
    mode: state.mode,
    profile: elements.profile.value,
    prompt: elements.prompt.value.trim(),
    width: Number(elements.width.value),
    height: Number(elements.height.value),
    duration: Number(elements.duration.value),
    seed: parseSeed(),
    ref_image_size: elements.refImageSize.value,
  };
  if (state.mode === "i2v") {
    payload.first_frame = state.assets.first_frame?.token || null;
    payload.last_frame = state.assets.last_frame?.token || null;
  } else if (state.mode === "r2v") {
    payload.ref_images = state.assets.ref_images.map((asset) => asset.token);
    payload.ref_videos = state.assets.ref_videos.map((asset) => asset.token);
    payload.ref_video_audios = state.assets.ref_videos.map((asset) => asset.soundtrack?.token || null);
    payload.ref_audios = state.assets.ref_audios.map((asset) => asset.token);
  }
  return payload;
}

async function submitRender(event) {
  event.preventDefault();
  if (!validateForm({ announce: true })) return;
  elements.renderButton.disabled = true;
  setFormMessage("Validating the native H3 graph…");
  try {
    const result = await api("/api/renders", { method: "POST", body: JSON.stringify(renderPayload()) });
    state.activeJobId = result.id;
    state.activeJob = { id: result.id, status: "pending", render: result.render, progress: { phase: "queued" } };
    showProgress(state.activeJob);
    setFormMessage("Render accepted and queued.", "success");
    await Promise.allSettled([refreshJobs(), pollActiveJob()]);
  } catch (error) {
    setFormMessage(error.message, "error");
  } finally {
    validateForm();
  }
}

function friendlyPhase(job) {
  const phase = job.progress?.phase || job.status;
  return {
    pending: ["Queued", "Waiting for the H3 engine."],
    submitting: ["Submitting", "Registering the native H3 graph with the engine."],
    queued: ["Queued", "Waiting for the H3 engine."],
    in_progress: ["Generating", "The model is working through the scene."],
    loading: ["Loading model", "Placing H3 stages across both GPUs and host memory."],
    executing: ["Preparing", "Conditioning picture, sound, and motion."],
    sampling: ["Sampling", "Denoising the joint video and audio latent."],
    finalizing: ["Finalizing", "Decoding frames, audio, and the MP4 container."],
    completed: ["Complete", "Your H3 video is ready."],
    complete: ["Complete", "Your H3 video is ready."],
    failed: ["Render failed", job.error || "The engine reported an error."],
    cancelled: ["Cancelled", "This render was stopped."],
    cancelling: ["Cancelling", "The engine is stopping this render."],
  }[phase] || ["Working", "The H3 engine is processing this render."];
}

function showProgress(job) {
  elements.outputVideo.pause();
  elements.outputVideo.hidden = true;
  elements.viewerActions.hidden = true;
  elements.outputMeta.hidden = true;
  elements.emptyStage.hidden = true;
  elements.renderProgress.hidden = false;
  const [title, detail] = friendlyPhase(job);
  elements.progressTitle.textContent = title;
  elements.progressDetail.textContent = detail;
  const percent = job.progress?.percent;
  elements.progressBar.style.width = percent == null ? (job.status === "completed" ? "100%" : "8%") : `${percent}%`;
  elements.progressPercent.textContent = percent == null ? "—" : `${Number(percent).toFixed(percent % 1 ? 1 : 0)}%`;
  elements.progressTime.textContent = job.render ? `${job.render.width}×${job.render.height} · ${job.render.length || alignedFrames(job.render.duration)} frames` : "H3 generation can take a while.";
  $("#cancelRender").hidden = !["pending", "in_progress", "cancelling"].includes(job.status);
}

function showOutput(job) {
  const output = job.outputs?.[0];
  if (!output) {
    showProgress(job);
    elements.progressDetail.textContent = job.status === "completed" ? "Render finished, but no saved MP4 was reported." : friendlyPhase(job)[1];
    return;
  }
  elements.renderProgress.hidden = true;
  elements.emptyStage.hidden = true;
  elements.outputVideo.hidden = false;
  if (elements.outputVideo.dataset.url !== output.url) {
    elements.outputVideo.dataset.url = output.url;
    elements.outputVideo.src = output.url;
    elements.outputVideo.load();
  }
  elements.viewerActions.hidden = false;
  elements.downloadOutput.href = output.download_url;
  elements.outputMeta.hidden = false;
  elements.outputMeta.replaceChildren();
  const render = job.render || {};
  const items = [
    render.mode?.toUpperCase(),
    render.profile?.replaceAll("_", " "),
    render.width && render.height ? `${render.width}×${render.height}` : null,
    render.length ? `${render.length} frames · ${(render.length / 24).toFixed(2)} s` : null,
    render.seed ? `seed ${render.seed}` : null,
  ].filter(Boolean);
  for (const text of items) {
    const item = document.createElement("span");
    item.className = "meta-item";
    item.textContent = text;
    elements.outputMeta.append(item);
  }
}

async function pollActiveJob() {
  clearTimeout(state.jobTimer);
  if (!state.activeJobId) return;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(state.activeJobId)}`);
    state.activeJob = job;
    if (job.status === "completed" && job.outputs?.length) showOutput(job);
    else showProgress(job);
    if (["submitting", "pending", "in_progress", "cancelling"].includes(job.status)) {
      state.jobTimer = setTimeout(pollActiveJob, 1800);
    } else {
      await refreshJobs();
    }
  } catch (error) {
    elements.progressDetail.textContent = `Status temporarily unavailable: ${error.message}`;
    state.jobTimer = setTimeout(pollActiveJob, 4000);
  }
}

async function cancelActiveJob() {
  if (!state.activeJobId) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(state.activeJobId)}/cancel`, { method: "POST", body: "{}" });
    setFormMessage("Cancellation requested.");
    await pollActiveJob();
  } catch (error) {
    setFormMessage(error.message, "error");
  }
}

async function refreshJobs() {
  try {
    const result = await api("/api/jobs?scope=all&limit=24");
    state.jobs = result.jobs || [];
    renderJobs();
  } catch (error) {
    if (!state.health?.connected) {
      elements.jobList.innerHTML = '<p class="empty-list">Start the engine to view its render history.</p>';
    }
  }
}

function renderJobs() {
  elements.jobList.replaceChildren();
  if (!state.jobs.length) {
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = "No H3 renders yet.";
    elements.jobList.append(empty);
    return;
  }
  for (const job of state.jobs) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "job-card";
    button.innerHTML = `<span class="job-state ${escapeHtml(job.status)}" aria-hidden="true"></span><span class="job-copy"><strong>${escapeHtml(job.status.replaceAll("_", " "))}</strong><small>H3 render · ${escapeHtml(job.id.slice(0, 8))}</small></span><span class="job-time">${escapeHtml(formatJobTime(job.create_time))}</span>`;
    button.setAttribute("aria-label", `Open ${job.status.replaceAll("_", " ")} render ${job.id.slice(0, 8)}`);
    button.addEventListener("click", () => openJob(job.id));
    elements.jobList.append(button);
  }
}

function formatJobTime(value) {
  if (!value) return "";
  const date = new Date(Number(value));
  if (Number.isNaN(date.valueOf())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

async function openJob(jobId) {
  state.activeJobId = jobId;
  elements.emptyStage.hidden = true;
  elements.renderProgress.hidden = false;
  elements.progressTitle.textContent = "Loading render";
  elements.progressDetail.textContent = "Reading the engine’s job record.";
  await pollActiveJob();
  $("#viewerTitle").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
}

function bindEvents() {
  const theme = window.H3Theme?.getState();
  if (theme && elements.themeSelect) elements.themeSelect.value = theme.preference;
  document.documentElement.addEventListener("h3-theme-change", (event) => {
    if (elements.themeSelect) elements.themeSelect.value = event.detail.preference;
  });
  elements.themeSelect?.addEventListener("change", () => {
    const selected = window.H3Theme?.setPreference(elements.themeSelect.value);
    if (!selected || !elements.themeStatus) return;
    const label = selected.preference === "system"
      ? `System theme selected; currently using ${selected.resolved}.`
      : `${selected.resolved[0].toUpperCase()}${selected.resolved.slice(1)} theme selected.`;
    elements.themeStatus.textContent = label;
  });
  elements.form.addEventListener("submit", submitRender);
  elements.prompt.addEventListener("input", () => { updatePromptCount(); validateForm(); });
  elements.profile.addEventListener("change", updateProfile);
  elements.resolution.addEventListener("change", updateResolution);
  elements.width.addEventListener("input", () => { elements.resolution.value = "custom"; elements.customDimensions.hidden = false; validateForm(); });
  elements.height.addEventListener("input", () => { elements.resolution.value = "custom"; elements.customDimensions.hidden = false; validateForm(); });
  elements.duration.addEventListener("input", updateDuration);
  elements.seed.addEventListener("input", () => validateForm());
  $("#randomSeed").addEventListener("click", randomSeed);
  $("#promptRecipe").addEventListener("click", () => {
    if (elements.prompt.value.trim() && !confirm("Replace the current prompt with a structured shot recipe?")) return;
    elements.prompt.value = recipes[state.mode];
    updatePromptCount();
    validateForm();
    elements.prompt.focus();
  });
  $("#clearPrompt").addEventListener("click", () => { elements.prompt.value = ""; updatePromptCount(); validateForm(); elements.prompt.focus(); });
  $("#clearKeyframes").addEventListener("click", clearKeyframes);
  $("#clearReferences").addEventListener("click", clearReferences);
  $("#copyCommand").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(elements.engineCommand.textContent);
      $("#copyCommand").textContent = "Copied";
      setTimeout(() => { $("#copyCommand").textContent = "Copy"; }, 1600);
    } catch {
      setFormMessage("Could not access the clipboard; select the command manually.", "error");
    }
  });
  $("#refreshButton").addEventListener("click", () => Promise.allSettled([refreshHealth(), refreshJobs()]));
  $("#engineStatus").addEventListener("click", () => elements.engineCallout.scrollIntoView({ behavior: "smooth", block: "center" }));
  $("#refreshJobs").addEventListener("click", refreshJobs);
  $("#cancelRender").addEventListener("click", cancelActiveJob);
  for (const input of $$('input[data-single]')) input.addEventListener("change", () => handleSingleUpload(input));
  for (const input of $$('input[data-list]')) input.addEventListener("change", () => handleListUpload(input));
  for (const zone of $$(".drop-zone, .compact-drop")) {
    zone.addEventListener("dragenter", (event) => {
      event.preventDefault();
      zone.classList.add("dragging");
    });
    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      zone.classList.add("dragging");
    });
    zone.addEventListener("dragleave", (event) => {
      if (!event.relatedTarget || !zone.contains(event.relatedTarget)) zone.classList.remove("dragging");
    });
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      zone.classList.remove("dragging");
      const input = $("input[type=file]", zone);
      const files = [...(event.dataTransfer?.files || [])];
      if (!input || !files.length) return;
      const transfer = new DataTransfer();
      for (const file of input.multiple ? files : files.slice(0, 1)) transfer.items.add(file);
      input.files = transfer.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }
}

async function init() {
  bindEvents();
  updatePromptCount();
  try {
    setupConfig(await api("/api/config"));
  } catch (error) {
    setFormMessage(`Webapp configuration failed: ${error.message}`, "error");
    return;
  }
  await Promise.allSettled([refreshHealth(), refreshJobs()]);
  setInterval(refreshHealth, 10000);
  setInterval(refreshJobs, 7000);
}

init();
