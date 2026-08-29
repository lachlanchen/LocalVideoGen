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
  workflow: "single",
  series: {
    template: "lalachan",
    canonical: Array(7).fill(null),
    extras: { images: [], videos: [], audio: [] },
    shots: [],
    templateShots: { lalachan: null, world_travel: null, movie: null },
    templateContinuity: { lalachan: "3", world_travel: "2", movie: "3" },
    sceneUploadVersions: new Map(),
    assetValidation: "verified",
    assetValidationVersion: 0,
    uploading: 0,
    record: null,
    serverDraftId: null,
    library: [],
    pollTimer: null,
    saveTimer: null,
    initialized: false,
  },
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
  singleWorkflowTab: $("#singleWorkflowTab"),
  seriesWorkflowTab: $("#seriesWorkflowTab"),
  singleComposer: $("#singleComposer"),
  seriesComposer: $("#seriesComposer"),
  singleOutputWorkspace: $("#singleOutputWorkspace"),
  seriesOutputWorkspace: $("#seriesOutputWorkspace"),
  seriesTitle: $("#seriesTitle"),
  seriesBrief: $("#seriesBrief"),
  seriesCastTitle: $("#seriesCastTitle"),
  seriesCastDescription: $("#seriesCastDescription"),
  seriesProfile: $("#seriesProfile"),
  seriesResolution: $("#seriesResolution"),
  seriesContinuity: $("#seriesContinuity"),
  seriesRefImageSize: $("#seriesRefImageSize"),
  canonicalReferenceGrid: $("#canonicalReferenceGrid"),
  worldTravelIdentityGuard: $("#worldTravelIdentityGuard"),
  seriesMoreReferencesSummary: $("#seriesMoreReferencesSummary"),
  seriesExtraImagesDrop: $("#seriesExtraImagesDrop"),
  seriesExtraImages: $("#seriesExtraImages"),
  seriesExtraAssetList: $("#seriesExtraAssetList"),
  seriesAssetCount: $("#seriesAssetCount"),
  seriesShotList: $("#seriesShotList"),
  seriesShotCount: $("#seriesShotCount"),
  seriesDurationTotal: $("#seriesDurationTotal"),
  seriesPreflight: $("#seriesPreflight"),
  seriesFormStatus: $("#seriesFormStatus"),
  startSeries: $("#startSeries"),
  seriesStartSummary: $("#seriesStartSummary"),
  seriesDraftStatus: $("#seriesDraftStatus"),
  seriesSummaryTitle: $("#seriesSummaryTitle"),
  seriesSummaryText: $("#seriesSummaryText"),
  seriesRecordError: $("#seriesRecordError"),
  seriesSummaryMetrics: $("#seriesSummaryMetrics"),
  seriesStatusBadge: $("#seriesStatusBadge"),
  seriesControls: $("#seriesControls"),
  pauseSeries: $("#pauseSeries"),
  resumeSeries: $("#resumeSeries"),
  startSavedSeries: $("#startSavedSeries"),
  retrySeriesFinalization: $("#retrySeriesFinalization"),
  cancelSeriesActive: $("#cancelSeriesActive"),
  seriesTimeline: $("#seriesTimeline"),
  seriesFinalPanel: $("#seriesFinalPanel"),
  seriesFinalVideo: $("#seriesFinalVideo"),
  downloadSeriesFinal: $("#downloadSeriesFinal"),
  seriesLibrary: $("#seriesLibrary"),
};

const SERIES_DRAFT_KEY = "h3-studio-series-draft-v1";
const SERIES_WORKFLOW_KEY = "h3-studio-workflow-v1";
const LALACHAN_REFERENCES = [
  ["Words card", "The episode's exact word or message card"],
  ["Zhuangzi Robot", "White robot identity and LazyingArt mark"],
  ["LightMind glasses", "Wearable display design and materials"],
  ["Patchwork notebook", "Recurring notebook and its cover"],
  ["Rara Xia", "Male giant panda hero and original outfit"],
  ["Aya Chan", "Female red panda hero and original outfit"],
  ["Sasa Kun", "Human boy, visible face, panda hoodie"],
];
const WORLD_TRAVEL_REFERENCES = LALACHAN_REFERENCES;
const WORLD_TRAVEL_OPENING_ONLY_REFERENCES = [
  "Words card",
  "LightMind glasses",
  "Patchwork notebook",
];
const MOVIE_REFERENCES = [
  ["Main character", "Primary face, costume, or subject"],
  ["Supporting character", "Second identity or object"],
  ["World & location", "Set, environment, or visual language"],
  ["Key prop", "An object that must remain recognizable"],
];
const SERIES_TEMPLATES = ["lalachan", "world_travel", "movie"];
const SERIES_TEMPLATE_LABELS = {
  lalachan: "LALACHAN Series",
  world_travel: "World Travel",
  movie: "My Movie",
};
const SERIES_TEMPLATE_TITLES = {
  lalachan: "New LALACHAN episode",
  world_travel: "New World Travel episode",
  movie: "My movie",
};

function defaultContinuityForTemplate(template) {
  return template === "world_travel" ? "2" : "3";
}

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
  setupSeriesConfig(config);
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
  for (const option of [...elements.seriesProfile.options]) {
    if (option.dataset.dual === "true") option.disabled = connected && count < 2;
  }
  if (state.series.initialized) updateSeriesReview();
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

function newLocalId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const values = new Uint32Array(4);
  crypto.getRandomValues(values);
  return [...values].map((value) => value.toString(16).padStart(8, "0")).join("-");
}

function defaultSeriesShots(template = "lalachan") {
  const seeds = ["20260828", "20260829", "20260830", "20260831", "20260832", "20260833"];
  let source;
  if (template === "lalachan") {
    source = [
      ["The signal", `One continuous cinematic opening shot. Continue the LALACHAN world with all supplied identities exact. Establish the place and a clear problem. Keep <Picture 5> Rara Xia, <Picture 6> Aya Chan, <Picture 7> Sasa Kun as a visibly human boy, and <Picture 2> Zhuangzi Robot distinct and readable. Let one character discover the clue on <Picture 1>. Natural Chinese dialogue, accurate lip sync, stereo ambience and tactile sound effects. No subtitles, narration, interface text, duplicate characters, or extra cast.`],
      ["Working together", `One continuous medium-wide shot. Continue the previous scene without replaying its completed action. Keep the same time of day, geography, costumes, scale, and four distinct identities from <Picture 2>, <Picture 5>, <Picture 6>, and <Picture 7>. The team solves the problem through a visible sequence of actions using <Picture 3> or <Picture 4>. Give each action a clear cause and effect. Natural Chinese dialogue, native stereo ambience, precise prop sounds. No subtitles, narration, duplicate characters, or unexplained cuts.`],
      ["A warm next clue", `A graceful closing shot that continues the previous scene without replaying it. Preserve every character and prop identity. Resolve the immediate problem, then reveal one small visual clue for the next LALACHAN episode. End on a strong, calm composition that can become the next episode's opening reference. Warm natural Chinese conversation, accurate lip sync, stereo environmental sound and a restrained musical lift. No subtitles, narration, interface text, duplicate characters, or new cast.`],
    ];
  } else if (template === "world_travel") {
    source = [
      ["Arrival and question", `Use <Picture 8> only as this shot's destination, architecture, terrain, light, and atmosphere anchor. Open with a motivated arrival and one visible question that starts the journey. Introduce the route through an action with <Picture 4> or <Picture 1>, not exposition. Keep the four travelers exact. Natural concise Chinese dialogue, accurate lip sync, native stereo ambience, and tactile travel sounds. No subtitles, narration, extra cast, checklist montage, or borrowed action from an earlier episode.`],
      ["The oldest layer", `Treat <Picture 9> as the already-completed previous moment. Begin the next unseen action at a calm, natural pace. Let <Picture 8> reveal one important historical layer through something the travelers can see, touch, cross, or compare. Turn one character's observation into a short question and a natural answer; show cause and effect instead of reciting facts. Preserve route, carried props, wardrobe, screen direction, voices, and all four identities. No subtitles, narration, or unrelated landmarks.`],
      ["People who made it", `Treat <Picture 9> as already completed and begin the next unseen part of the same journey at a calm, natural pace. Use this shot's <Picture 8> to connect one maker, craft, artwork, building method, or local invention to a visible present-day detail. Give each character a distinct, brief reaction and keep the camera movement physically coherent. Natural Chinese conversation, accurate lip sync, local ambience, and precise prop sounds. Do not turn the scene into a lecture or copy an earlier episode's composition.`],
      ["Everyday life", `Treat <Picture 9> as already completed; begin the next unseen action directly, without replay, at a calm, natural pace. In the place anchored by <Picture 8>, let the travelers encounter one ordinary custom, street, market, food, or social ritual that makes the destination feel lived in. The discovery must advance the same question or journey rather than pause for a travel advertisement. Preserve identities, route logic, time progression, wardrobe, voices, and carried props. No subtitles, narration, stereotypes, or unexplained cuts.`],
      ["Road to the final place", `Treat <Picture 9> as the finished prior movement; do not extend it. Begin the next travel leg at a natural pace, using <Picture 8> only for the new stop. Make the geographic transition legible through a road, rail, river, path, doorway, or motivated visual match. Reveal one essential sight and one concise historical connection through action and dialogue. Keep screen direction, time progression, cast identity, voices, wardrobe, and props coherent. No tourist-checklist montage, narration, or borrowed plot direction.`],
      ["Living memory", `Treat <Picture 9> as already completed and begin the next unseen moment in the final place anchored by <Picture 8>. Resolve the opening question by showing how the destination's history remains alive in a present sound, object, street, craft, meal, or shared gesture. Complete the emotional thread through that visible detail, then end on a calm final composition suitable for future continuity. Warm concise Chinese dialogue, accurate lip sync, stereo place ambience. No subtitles, narration, new cast, or forced sequel tease.`],
    ];
  } else {
    source = [
      ["Opening image", `A cinematic opening shot. Establish the main character, location, time of day, and the question that pulls us forward. Describe exact blocking, camera movement, lighting, dialogue, ambience, and sound effects. Preserve every supplied reference identity. No subtitles, narration, interface text, duplicate subjects, or unexplained cuts.`],
      ["Change", `Continue the previous scene without replaying its completed action. Show one meaningful choice or change through a clear sequence of visible actions. Preserve geography, screen direction, wardrobe, lighting logic, and all supplied identities. Direct the camera, dialogue, ambience, and tactile sounds. No subtitles or interface text.`],
      ["Resolution", `Continue exactly from the previous shot. Resolve the immediate action and land on a memorable final image. Leave a subtle visual possibility for another scene while making this sequence feel complete. Preserve all reference identities, native stereo sound, and natural dialogue. No subtitles or interface text.`],
    ];
  }
  return source.map(([title, prompt], index) => ({
    id: newLocalId(),
    title,
    prompt,
    duration: 10,
    seed: seeds[index],
    sceneReference: null,
    omitSharedImageLabels: template === "world_travel" && index > 0 ? [...WORLD_TRAVEL_OPENING_ONLY_REFERENCES] : [],
  }));
}

function cloneSeriesShots(shots) {
  return shots.map((shot) => ({
    ...shot,
    sceneReference: shot.sceneReference ? { ...shot.sceneReference, metadata: { ...(shot.sceneReference.metadata || {}) } } : null,
    omitSharedImageLabels: Array.isArray(shot.omitSharedImageLabels) ? [...shot.omitSharedImageLabels] : [],
  }));
}

function normalizedWorldTravelOmissions(shot, index) {
  if (index === 0) return [];
  const saved = Array.isArray(shot?.omitSharedImageLabels)
    ? shot.omitSharedImageLabels
    : Array.isArray(shot?.omit_shared_image_labels)
      ? shot.omit_shared_image_labels
      : null;
  if (saved === null) return [...WORLD_TRAVEL_OPENING_ONLY_REFERENCES];
  const selected = new Set(saved.filter((label) => typeof label === "string"));
  return WORLD_TRAVEL_OPENING_ONLY_REFERENCES.filter((label) => selected.has(label));
}

function worldTravelOmissionsForShot(shot, index) {
  return normalizedWorldTravelOmissions(shot, index);
}

function normalizeDraftShots(shots, template = "lalachan") {
  if (!Array.isArray(shots)) return null;
  const normalized = shots.slice(0, 12).map((shot, index) => ({
    id: typeof shot?.id === "string" ? shot.id : newLocalId(),
    title: String(shot?.title || `Shot ${index + 1}`).slice(0, 120),
    prompt: String(shot?.prompt || "").slice(0, 10000),
    duration: Math.min(15, Math.max(5, Number(shot?.duration) || 10)),
    seed: /^[0-9]+$/.test(String(shot?.seed || "")) ? String(shot.seed) : String(20260828 + index),
    sceneReference: normalizeDraftAsset(shot?.sceneReference),
    omitSharedImageLabels: template === "world_travel" ? normalizedWorldTravelOmissions(shot, index) : [],
  }));
  return normalized.length >= 2 ? normalized : null;
}

function seriesReferenceLabels() {
  if (state.series.template === "movie") return MOVIE_REFERENCES;
  if (state.series.template === "world_travel") return WORLD_TRAVEL_REFERENCES;
  return LALACHAN_REFERENCES;
}

function isWorldTravel() {
  return state.series.template === "world_travel";
}

function seriesTemplateLabel(template = state.series.template) {
  return SERIES_TEMPLATE_LABELS[template] || "Series";
}

function sceneReferenceLabel(shot, index = state.series.shots.indexOf(shot)) {
  const title = String(shot?.title || `Shot ${index + 1}`).trim();
  return `${title || `Shot ${index + 1}`} · location plate`;
}

function findSeriesShotById(id) {
  const active = state.series.shots.find((shot) => shot.id === id);
  if (active) return active;
  for (const shots of Object.values(state.series.templateShots)) {
    const found = Array.isArray(shots) ? shots.find((shot) => shot.id === id) : null;
    if (found) return found;
  }
  return null;
}

function sceneReferencesInDrafts() {
  const shots = [...state.series.shots];
  for (const templateShots of Object.values(state.series.templateShots)) {
    if (Array.isArray(templateShots)) shots.push(...templateShots);
  }
  return shots.map((shot) => shot.sceneReference).filter(Boolean);
}

function normalizeDraftAsset(asset) {
  if (!asset || typeof asset !== "object" || typeof asset.token !== "string" || !asset.token) return null;
  return {
    token: asset.token,
    name: typeof asset.name === "string" ? asset.name : "Uploaded reference",
    size: Number.isFinite(Number(asset.size)) ? Number(asset.size) : 0,
    metadata: asset.metadata && typeof asset.metadata === "object" ? asset.metadata : {},
    label: typeof asset.label === "string" ? asset.label : "",
    soundtrack: normalizeDraftAsset(asset.soundtrack),
  };
}

function restoredSeriesUploads() {
  const uploads = [];
  const seen = new Set();
  const add = (asset, kind) => {
    const key = asset?.token ? `${kind}:${asset.token}` : "";
    if (key && !seen.has(key)) {
      seen.add(key);
      uploads.push({ token: asset.token, kind });
    }
  };
  state.series.canonical.forEach((asset) => add(asset, "image"));
  state.series.extras.images.forEach((asset) => add(asset, "image"));
  state.series.extras.videos.forEach((asset) => {
    add(asset, "video");
    add(asset.soundtrack, "audio");
  });
  state.series.extras.audio.forEach((asset) => add(asset, "audio"));
  sceneReferencesInDrafts().forEach((asset) => add(asset, "image"));
  return uploads;
}

async function validateRestoredSeriesAssets() {
  const version = ++state.series.assetValidationVersion;
  const uploads = restoredSeriesUploads();
  if (!uploads.length) {
    state.series.assetValidation = "verified";
    if (state.series.initialized) updateSeriesReview();
    return;
  }
  const checkedTokens = new Set(uploads.map((upload) => upload.token));
  state.series.assetValidation = "checking";
  elements.seriesDraftStatus.textContent = "Checking saved references…";
  updateSeriesReview();
  try {
    const result = await api("/api/uploads/validate", {
      method: "POST",
      body: JSON.stringify({ uploads }),
    });
    if (version !== state.series.assetValidationVersion) return;
    const valid = new Set(Array.isArray(result.valid) ? result.valid : []);
    const invalidTokens = new Set([...checkedTokens].filter((token) => !valid.has(token)));
    const expired = invalidTokens.size;
    const keep = (asset) => {
      if (!asset || !invalidTokens.has(asset.token)) return asset;
      return null;
    };
    state.series.canonical = state.series.canonical.map(keep);
    state.series.extras.images = state.series.extras.images.map(keep).filter(Boolean);
    state.series.extras.videos = state.series.extras.videos.map((asset) => {
      const video = keep(asset);
      if (!video) return null;
      video.soundtrack = keep(video.soundtrack);
      return video;
    }).filter(Boolean);
    state.series.extras.audio = state.series.extras.audio.map(keep).filter(Boolean);
    const cleanShotScenes = (shots) => {
      if (!Array.isArray(shots)) return shots;
      shots.forEach((shot) => { shot.sceneReference = keep(shot.sceneReference); });
      return shots;
    };
    cleanShotScenes(state.series.shots);
    for (const template of SERIES_TEMPLATES) cleanShotScenes(state.series.templateShots[template]);
    state.series.assetValidation = "verified";
    renderCanonicalReferences();
    renderSeriesExtraAssets();
    renderSeriesShots();
    if (expired) {
      setSeriesMessage(`${expired} saved reference${expired === 1 ? " has" : "s have"} expired. Upload ${expired === 1 ? "it" : "them"} again before generating.`, "error");
      saveSeriesDraftSoon();
    } else {
      elements.seriesDraftStatus.textContent = "Draft restored · references verified";
    }
  } catch (error) {
    if (version !== state.series.assetValidationVersion) return;
    state.series.assetValidation = "failed";
    updateSeriesReview();
    setSeriesMessage(`Saved references could not be verified: ${error.message}`, "error");
    elements.seriesDraftStatus.textContent = "Draft restored · reference check failed";
  }
}

function refreshSeriesAssetValidation() {
  if (state.series.uploading === 0) void validateRestoredSeriesAssets();
}

function setupSeriesConfig(config) {
  elements.seriesProfile.replaceChildren();
  for (const profile of config.profiles) {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = profile.label;
    option.dataset.dual = String(profile.dual_gpu);
    elements.seriesProfile.append(option);
  }
  elements.seriesProfile.value = config.defaults.profile;

  elements.seriesResolution.replaceChildren();
  for (const resolution of config.resolutions) {
    const option = document.createElement("option");
    option.value = resolution.id;
    option.textContent = resolution.label;
    option.dataset.width = String(resolution.width);
    option.dataset.height = String(resolution.height);
    elements.seriesResolution.append(option);
  }
  elements.seriesResolution.value = config.resolutions.find((item) => item.width === config.defaults.width && item.height === config.defaults.height)?.id || config.resolutions[0]?.id;

  restoreSeriesDraft();
  if (!state.series.shots.length) state.series.shots = defaultSeriesShots(state.series.template);
  state.series.templateShots[state.series.template] = cloneSeriesShots(state.series.shots);
  renderSeriesTemplate();
  renderCanonicalReferences();
  renderSeriesExtraAssets();
  renderSeriesShots();
  state.series.initialized = true;
  void validateRestoredSeriesAssets();
  const savedWorkflow = (() => { try { return localStorage.getItem(SERIES_WORKFLOW_KEY); } catch { return null; } })();
  if (savedWorkflow === "series") switchWorkflow("series", { focus: false });
  else updateSeriesReview();
}

function restoreSeriesDraft() {
  let draft = null;
  try { draft = JSON.parse(localStorage.getItem(SERIES_DRAFT_KEY) || "null"); } catch { draft = null; }
  if (!draft || typeof draft !== "object" || !SERIES_TEMPLATES.includes(draft.template)) return;
  state.series.template = draft.template;
  state.series.canonical = Array(7).fill(null).map((_, index) => normalizeDraftAsset(draft.canonical?.[index]));
  for (const key of ["images", "videos", "audio"]) {
    state.series.extras[key] = Array.isArray(draft.extras?.[key]) ? draft.extras[key].map(normalizeDraftAsset).filter(Boolean) : [];
  }
  for (const template of SERIES_TEMPLATES) {
    state.series.templateShots[template] = normalizeDraftShots(draft.templateShots?.[template], template);
    const savedContinuity = String(draft.templateContinuity?.[template] ?? "");
    if (["0", "2", "3", "4"].includes(savedContinuity) && !(template === "world_travel" && savedContinuity === "0")) {
      state.series.templateContinuity[template] = savedContinuity;
    }
  }
  state.series.shots = normalizeDraftShots(draft.shots, draft.template)
    || cloneSeriesShots(state.series.templateShots[state.series.template] || []);
  if (typeof draft.title === "string") elements.seriesTitle.value = draft.title.slice(0, 120);
  if (typeof draft.brief === "string") elements.seriesBrief.value = draft.brief.slice(0, 2000);
  if (draft.profile && [...elements.seriesProfile.options].some((option) => option.value === draft.profile)) elements.seriesProfile.value = draft.profile;
  if (draft.resolution && [...elements.seriesResolution.options].some((option) => option.value === draft.resolution)) elements.seriesResolution.value = draft.resolution;
  if (["0", "2", "3", "4"].includes(String(draft.continuity))) {
    const restoredContinuity = state.series.template === "world_travel" && String(draft.continuity) === "0"
      ? "2"
      : String(draft.continuity);
    elements.seriesContinuity.value = restoredContinuity;
    state.series.templateContinuity[state.series.template] = restoredContinuity;
  } else {
    elements.seriesContinuity.value = state.series.templateContinuity[state.series.template]
      || defaultContinuityForTemplate(state.series.template);
  }
  if (["max", "match"].includes(draft.refImageSize)) elements.seriesRefImageSize.value = draft.refImageSize;
  if (typeof draft.serverDraftId === "string") state.series.serverDraftId = draft.serverDraftId;
}

function saveSeriesDraftSoon() {
  if (!state.series.initialized) return;
  clearTimeout(state.series.saveTimer);
  elements.seriesDraftStatus.textContent = "Saving draft…";
  state.series.saveTimer = setTimeout(() => {
    state.series.templateShots[state.series.template] = cloneSeriesShots(state.series.shots);
    const draft = {
      template: state.series.template,
      title: elements.seriesTitle.value,
      brief: elements.seriesBrief.value,
      profile: elements.seriesProfile.value,
      resolution: elements.seriesResolution.value,
      continuity: elements.seriesContinuity.value,
      templateContinuity: state.series.templateContinuity,
      refImageSize: elements.seriesRefImageSize.value,
      canonical: state.series.canonical,
      extras: state.series.extras,
      shots: state.series.shots,
      templateShots: state.series.templateShots,
      serverDraftId: state.series.serverDraftId,
    };
    try {
      localStorage.setItem(SERIES_DRAFT_KEY, JSON.stringify(draft));
      elements.seriesDraftStatus.textContent = "Draft saved in this browser";
    } catch {
      elements.seriesDraftStatus.textContent = "Draft could not be saved";
    }
  }, 320);
}

function setSeriesMessage(message = "", kind = "") {
  elements.seriesFormStatus.textContent = message;
  elements.seriesFormStatus.className = `form-status${kind ? ` ${kind}` : ""}`;
}

function switchWorkflow(workflow, { focus = true } = {}) {
  const series = workflow === "series";
  state.workflow = series ? "series" : "single";
  elements.singleWorkflowTab.setAttribute("aria-pressed", String(!series));
  elements.seriesWorkflowTab.setAttribute("aria-pressed", String(series));
  elements.singleComposer.hidden = series;
  elements.seriesComposer.hidden = !series;
  elements.singleOutputWorkspace.hidden = series;
  elements.seriesOutputWorkspace.hidden = !series;
  $("#studioTitle").textContent = series ? "Direct a connected story." : "Direct the whole scene.";
  try { localStorage.setItem(SERIES_WORKFLOW_KEY, state.workflow); } catch { /* Private browsing may deny storage. */ }
  if (series) {
    updateSeriesReview();
    refreshSeriesLibrary();
  }
  if (focus) (series ? elements.seriesTitle : elements.prompt).focus({ preventScroll: true });
}

function applySeriesTemplate(template) {
  if (!SERIES_TEMPLATES.includes(template) || template === state.series.template) return;
  const previous = state.series.template;
  state.series.templateShots[previous] = cloneSeriesShots(state.series.shots);
  state.series.templateContinuity[previous] = elements.seriesContinuity.value;
  state.series.template = template;
  state.series.shots = cloneSeriesShots(state.series.templateShots[template] || defaultSeriesShots(template));
  state.series.templateShots[template] = cloneSeriesShots(state.series.shots);
  if (elements.seriesTitle.value === SERIES_TEMPLATE_TITLES[previous]) {
    elements.seriesTitle.value = SERIES_TEMPLATE_TITLES[template];
  }
  const nextContinuity = state.series.templateContinuity[template] || defaultContinuityForTemplate(template);
  elements.seriesContinuity.value = isWorldTravel() && nextContinuity === "0" ? "2" : nextContinuity;
  state.series.templateContinuity[template] = elements.seriesContinuity.value;
  renderSeriesTemplate();
  renderCanonicalReferences();
  renderSeriesExtraAssets();
  renderSeriesShots();
  if (!isWorldTravel() && seriesSharedImageCount() > 8) {
    setSeriesMessage("This template exposes more than eight shared pictures. Remove extras before preflight can pass; no upload was deleted.", "error");
  } else {
    const parked = isWorldTravel() && state.series.extras.images.length
      ? ` ${state.series.extras.images.length} extra picture${state.series.extras.images.length === 1 ? " is" : "s are"} parked and will not shift P8/P9.`
      : "";
    setSeriesMessage(`Switched to ${seriesTemplateLabel(template)}. Your ${seriesTemplateLabel(previous)} storyboard is preserved.${parked}`, "success");
  }
  saveSeriesDraftSoon();
  refreshSeriesAssetValidation();
}

function renderSeriesTemplate() {
  for (const radio of $$('input[name="seriesTemplate"]')) {
    radio.checked = radio.value === state.series.template;
    radio.closest(".preset-card")?.classList.toggle("selected", radio.checked);
  }
  const travel = isWorldTravel();
  if (travel && elements.seriesContinuity.value === "0") {
    elements.seriesContinuity.value = "2";
    state.series.templateContinuity.world_travel = "2";
  }
  elements.worldTravelIdentityGuard.hidden = !travel;
  elements.seriesExtraImagesDrop.hidden = travel;
  elements.seriesExtraImages.disabled = travel;
  elements.seriesMoreReferencesSummary.textContent = travel
    ? "Add identity/voice guidance from earlier episodes"
    : "Add movement, voice, music, or extra pictures";
  const off = elements.seriesContinuity.querySelector('option[value="0"]');
  if (off) off.disabled = travel;
  elements.seriesCastTitle.textContent = travel ? "Shared cast & identity" : "Shared cast & world";
  elements.seriesCastDescription.textContent = travel
    ? "P1–P7 stay fixed across the journey. Each shot gets its own location plate as P8; continuity arrives as P9."
    : "Upload once. Every shot receives the shared references, and each shot card shows its exact prompt tags.";
}

function bindFileDrop(zone, input) {
  for (const eventName of ["dragenter", "dragover"]) {
    zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      zone.classList.add("dragging");
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    });
  }
  zone.addEventListener("dragleave", (event) => {
    if (!event.relatedTarget || !zone.contains(event.relatedTarget)) zone.classList.remove("dragging");
  });
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("dragging");
    const files = [...(event.dataTransfer?.files || [])];
    if (!files.length) return;
    const transfer = new DataTransfer();
    for (const file of input.multiple ? files : files.slice(0, 1)) transfer.items.add(file);
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function renderCanonicalReferences() {
  elements.canonicalReferenceGrid.replaceChildren();
  seriesReferenceLabels().forEach(([label, hint], index) => {
    const asset = state.series.canonical[index];
    const slot = document.createElement("div");
    slot.className = `canonical-slot${asset ? " ready" : ""}`;

    const target = document.createElement("label");
    target.className = "canonical-slot-target";
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/png,image/jpeg,image/webp,image/bmp";
    input.setAttribute("aria-label", `${asset ? "Replace" : "Upload"} ${label} reference`);
    input.addEventListener("change", () => handleCanonicalUpload(index, input, slot));
    const number = document.createElement("span");
    number.className = "canonical-slot-number";
    number.textContent = `P${index + 1}`;
    const copy = document.createElement("span");
    copy.className = "canonical-slot-copy";
    const strong = document.createElement("strong");
    strong.textContent = label;
    const small = document.createElement("small");
    small.textContent = asset ? `${asset.name} · ${formatBytes(asset.size)}` : hint;
    copy.append(strong, small);
    target.append(input, number, copy);
    slot.append(target);

    if (asset) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "canonical-slot-remove";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `Remove ${label} reference`);
      remove.addEventListener("click", () => {
        state.series.canonical[index] = null;
        renderCanonicalReferences();
        updateSeriesReview();
        saveSeriesDraftSoon();
        refreshSeriesAssetValidation();
      });
      slot.append(remove);
    }
    bindFileDrop(slot, input);
    elements.canonicalReferenceGrid.append(slot);
  });
  updateSeriesAssetCount();
}

async function handleCanonicalUpload(index, input, slot) {
  const file = input.files?.[0];
  if (!file) return;
  const replacingExisting = Boolean(state.series.canonical[index]);
  if (seriesSharedImageCount() + (replacingExisting ? 0 : 1) > 8) {
    input.value = "";
    setSeriesMessage("Eight shared pictures are already in use; the ninth H3 picture slot stays reserved for continuity.", "error");
    updateSeriesReview();
    return;
  }
  slot.classList.add("uploading");
  state.series.uploading += 1;
  setSeriesMessage(`Uploading ${file.name}…`);
  updateSeriesReview();
  try {
    const record = await uploadFile(file, "image");
    record.label = seriesReferenceLabels()[index]?.[0] || `Picture ${index + 1}`;
    state.series.canonical[index] = record;
    setSeriesMessage(`${record.label} is ready.`, "success");
  } catch (error) {
    setSeriesMessage(error.message, "error");
  } finally {
    state.series.uploading -= 1;
    renderCanonicalReferences();
    updateSeriesReview();
    saveSeriesDraftSoon();
    refreshSeriesAssetValidation();
  }
}

function seriesReadyAssets() {
  const visibleCanonical = state.series.canonical.slice(0, seriesReferenceLabels().length).filter(Boolean);
  const activeExtraImages = isWorldTravel() ? 0 : state.series.extras.images.length;
  return visibleCanonical.length + activeExtraImages + state.series.extras.videos.length + state.series.extras.audio.length;
}

function seriesSharedImageCount() {
  const activeExtraImages = isWorldTravel() ? 0 : state.series.extras.images.length;
  return state.series.canonical.slice(0, seriesReferenceLabels().length).filter(Boolean).length + activeExtraImages;
}

function seriesReadySceneCount() {
  return isWorldTravel() ? state.series.shots.filter((shot) => shot.sceneReference?.token).length : 0;
}

function updateSeriesAssetCount() {
  const count = seriesReadyAssets();
  elements.seriesAssetCount.textContent = isWorldTravel() ? `${count} shared · ${seriesReadySceneCount()} scenes` : `${count} ready`;
}

async function handleSeriesExtraUpload(input) {
  const kind = input.dataset.seriesKind;
  const key = kind === "image" ? "images" : kind === "video" ? "videos" : "audio";
  const files = [...(input.files || [])];
  if (!files.length) return;
  if (isWorldTravel() && key === "images") {
    input.value = "";
    setSeriesMessage("World Travel reserves P8 for each shot's own scene plate and P9 for continuity. Add location pictures inside the shot cards.", "error");
    return;
  }
  const canonicalCount = state.series.canonical.slice(0, seriesReferenceLabels().length).filter(Boolean).length;
  const limits = { images: Math.max(0, 8 - canonicalCount), videos: 2, audio: 3 };
  const remaining = Math.max(0, limits[key] - state.series.extras[key].length);
  if (!remaining) {
    const message = key === "images" ? "Eight shared pictures are available; the ninth stays reserved for the prior shot's final frame."
      : key === "videos" ? "Two shared video slots are available; the third stays reserved for shot continuity."
        : "H3 supports three shared audio references.";
    setSeriesMessage(message, "error");
    input.value = "";
    return;
  }
  state.series.uploading += 1;
  updateSeriesReview();
  let uploaded = 0;
  let failure = null;
  const skipped = Math.max(0, files.length - remaining);
  for (const file of files.slice(0, remaining)) {
    setSeriesMessage(`Uploading ${file.name}…`);
    try {
      const record = await uploadFile(file, kind);
      record.label = file.name.replace(/\.[^.]+$/, "").slice(0, 80);
      if (key === "videos") record.soundtrack = null;
      state.series.extras[key].push(record);
      uploaded += 1;
      renderSeriesExtraAssets();
    } catch (error) {
      failure = error;
      break;
    }
  }
  state.series.uploading -= 1;
  input.value = "";
  if (failure) {
    setSeriesMessage(`${uploaded ? `${uploaded} reference${uploaded === 1 ? " was" : "s were"} added. ` : ""}${failure.message}`, "error");
  } else if (skipped) {
    setSeriesMessage(`${uploaded} reference${uploaded === 1 ? " was" : "s were"} added; ${skipped} did not fit the remaining ${key} slots.`, "error");
  } else {
    setSeriesMessage(`${uploaded} shared reference${uploaded === 1 ? " is" : "s are"} ready.`, "success");
  }
  renderCanonicalReferences();
  updateSeriesReview();
  saveSeriesDraftSoon();
  refreshSeriesAssetValidation();
}

async function attachSeriesVideoSoundtrack(index) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "audio/*,.flac,.m4a,.opus";
  input.addEventListener("change", async () => {
    const file = input.files?.[0];
    const video = state.series.extras.videos[index];
    if (!file || !video) return;
    state.series.uploading += 1;
    updateSeriesReview();
    setSeriesMessage(`Uploading ${file.name}…`);
    try {
      video.soundtrack = await uploadFile(file, "audio");
      setSeriesMessage(`Soundtrack attached to ${video.name}.`, "success");
      renderSeriesExtraAssets();
      saveSeriesDraftSoon();
    } catch (error) {
      setSeriesMessage(error.message, "error");
    } finally {
      state.series.uploading -= 1;
      updateSeriesReview();
      refreshSeriesAssetValidation();
    }
  }, { once: true });
  input.click();
}

function renderSeriesExtraAssets() {
  elements.seriesExtraAssetList.replaceChildren();
  const groups = [["images", "pic", "Picture"], ["videos", "vid", "Video"], ["audio", "aud", "Audio"]];
  for (const [key, short, label] of groups) {
    state.series.extras[key].forEach((asset, index) => {
      const card = $("#assetTemplate").content.firstElementChild.cloneNode(true);
      const parked = isWorldTravel() && key === "images";
      card.classList.toggle("parked", parked);
      $(".asset-kind", card).textContent = short;
      $(".asset-copy strong", card).textContent = asset.label || asset.name;
      $(".asset-copy small", card).textContent = parked
        ? `${label} · parked for this template · preserved for other drafts`
        : `${label} · ${asset.name} · ${formatBytes(asset.size)}`;
      $(".remove-asset", card).setAttribute("aria-label", `Remove ${asset.name}`);
      $(".remove-asset", card).addEventListener("click", () => {
        state.series.extras[key].splice(index, 1);
        renderSeriesExtraAssets();
        renderCanonicalReferences();
        updateSeriesReview();
        saveSeriesDraftSoon();
        refreshSeriesAssetValidation();
      });
      if (key === "videos") {
        const attach = document.createElement("button");
        attach.type = "button";
        attach.className = "text-button";
        attach.textContent = asset.soundtrack ? `Soundtrack: ${asset.soundtrack.name}` : "Attach separate soundtrack";
        attach.addEventListener("click", () => attachSeriesVideoSoundtrack(index));
        $(".asset-copy", card).append(attach);
      }
      elements.seriesExtraAssetList.append(card);
    });
  }
  updateSeriesAssetCount();
}

function actualShotSeconds(shot) {
  return alignedFrames(Number(shot.duration) || 5) / 24;
}

function seriesTotals() {
  const frames = state.series.shots.reduce((sum, shot) => sum + alignedFrames(Number(shot.duration) || 5), 0);
  return { frames, seconds: frames / 24 };
}

function updateShotState(id, key, value) {
  const shot = state.series.shots.find((item) => item.id === id);
  if (!shot) return;
  shot[key] = value;
  updateSeriesReview();
  saveSeriesDraftSoon();
}

function shotActionButton(label, text, handler, { disabled = false, className = "" } = {}) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `shot-icon-button${className ? ` ${className}` : ""}`;
  button.textContent = text;
  button.title = label;
  button.setAttribute("aria-label", label);
  button.disabled = disabled;
  button.addEventListener("click", handler);
  return button;
}

function reusableSceneReferences(currentShotId) {
  const unique = new Map();
  state.series.shots.forEach((shot, index) => {
    const asset = shot.sceneReference;
    if (shot.id === currentShotId || !asset?.token || unique.has(asset.token)) return;
    unique.set(asset.token, { asset, source: shot.title || `Shot ${index + 1}` });
  });
  return [...unique.values()];
}

function setShotSceneReference(shotId, asset, message) {
  const shot = findSeriesShotById(shotId);
  if (!shot) return;
  shot.sceneReference = asset ? { ...asset, metadata: { ...(asset.metadata || {}) } } : null;
  if (state.series.shots.some((item) => item.id === shotId)) renderSeriesShots();
  else updateSeriesReview();
  if (message) setSeriesMessage(message, "success");
  saveSeriesDraftSoon();
}

async function handleShotSceneUpload(shotId, input, zone) {
  const file = input.files?.[0];
  if (!file) return;
  const version = (state.series.sceneUploadVersions.get(shotId) || 0) + 1;
  state.series.sceneUploadVersions.set(shotId, version);
  state.series.uploading += 1;
  zone.classList.add("uploading");
  setSeriesMessage(`Uploading ${file.name} as this shot's P8 scene plate…`);
  updateSeriesReview();
  try {
    const record = await uploadFile(file, "image");
    const shot = findSeriesShotById(shotId);
    if (!shot || state.series.sceneUploadVersions.get(shotId) !== version) return;
    record.label = sceneReferenceLabel(shot);
    shot.sceneReference = record;
    setSeriesMessage(`${file.name} is locked to this shot as P8.`, "success");
  } catch (error) {
    if (state.series.sceneUploadVersions.get(shotId) === version) setSeriesMessage(error.message, "error");
  } finally {
    state.series.uploading -= 1;
    input.value = "";
    if (state.series.shots.some((shot) => shot.id === shotId)) renderSeriesShots();
    else updateSeriesReview();
    saveSeriesDraftSoon();
    refreshSeriesAssetValidation();
  }
}

function renderShotSceneReference(shot, index) {
  const asset = shot.sceneReference;
  const editor = document.createElement("section");
  editor.className = `scene-reference-editor${asset ? " ready" : " missing"}`;
  editor.setAttribute("aria-label", `Shot ${index + 1} scene reference`);

  const heading = document.createElement("div");
  heading.className = "scene-reference-heading";
  const tag = document.createElement("span");
  tag.className = "scene-reference-tag";
  tag.textContent = "P8";
  const copy = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = "Shot-only location plate";
  const small = document.createElement("small");
  small.textContent = asset
    ? `${asset.name} · ${formatBytes(asset.size)}`
    : "Required: architecture, terrain, light, and atmosphere for this shot";
  copy.append(strong, small);
  heading.append(tag, copy);

  const controls = document.createElement("div");
  controls.className = "scene-reference-controls";
  const zone = document.createElement("label");
  zone.className = "scene-reference-drop";
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/png,image/jpeg,image/webp,image/bmp";
  input.setAttribute("aria-label", `${asset ? "Replace" : "Upload"} Shot ${index + 1} P8 location plate`);
  input.addEventListener("change", () => handleShotSceneUpload(shot.id, input, zone));
  const uploadCopy = document.createElement("span");
  uploadCopy.textContent = asset ? "Replace plate" : "Upload scene plate";
  zone.append(input, uploadCopy);
  bindFileDrop(zone, input);
  controls.append(zone);

  const reusable = reusableSceneReferences(shot.id);
  if (reusable.length) {
    const reuse = document.createElement("select");
    reuse.className = "scene-reference-reuse";
    reuse.setAttribute("aria-label", `Reuse another scene plate for Shot ${index + 1}`);
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Reuse another shot's plate…";
    placeholder.selected = true;
    placeholder.disabled = true;
    reuse.append(placeholder);
    reusable.forEach(({ asset: candidate, source }) => {
      const option = document.createElement("option");
      option.value = candidate.token;
      option.textContent = `${source} · ${candidate.name}`;
      reuse.append(option);
    });
    reuse.addEventListener("change", () => {
      const selected = reusable.find(({ asset: candidate }) => candidate.token === reuse.value);
      if (!selected) return;
      setShotSceneReference(shot.id, selected.asset, `Reused ${selected.asset.name} as Shot ${index + 1}'s P8 plate.`);
      refreshSeriesAssetValidation();
    });
    controls.append(reuse);
  }

  if (asset) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "scene-reference-remove";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", `Remove Shot ${index + 1} P8 location plate`);
    remove.addEventListener("click", () => {
      state.series.sceneUploadVersions.set(shot.id, (state.series.sceneUploadVersions.get(shot.id) || 0) + 1);
      setShotSceneReference(shot.id, null);
      setSeriesMessage(`Shot ${index + 1} needs a new P8 scene plate. The uploaded file was not deleted.`, "error");
      refreshSeriesAssetValidation();
    });
    controls.append(remove);
  }

  const guard = document.createElement("p");
  guard.className = "scene-reference-guard";
  guard.textContent = "P8 controls only this place. It must not change the cast; the next destination gets its own P8.";
  editor.append(heading, controls, guard);
  return editor;
}

function renderShotReferencePolicy(shot, index) {
  const editor = document.createElement("section");
  editor.className = "shot-reference-policy";
  editor.setAttribute("aria-label", `Shot ${index + 1} shared reference policy`);

  const heading = document.createElement("div");
  heading.className = "shot-reference-policy-heading";
  const strong = document.createElement("strong");
  strong.textContent = "Opening-only reference guard";
  const small = document.createElement("small");
  heading.append(strong, small);
  editor.append(heading);

  if (index === 0) {
    small.textContent = "Shot 1 keeps all seven canonical pictures for the opening.";
    return editor;
  }

  const controls = document.createElement("div");
  controls.className = "shot-reference-policy-options";
  const updateCopy = () => {
    const count = worldTravelOmissionsForShot(shot, index).length;
    small.textContent = count
      ? `${count} opening-only picture${count === 1 ? " is" : "s are"} excluded from this H3 render.`
      : "All seven pictures will be sent; keep this only when the shot deliberately uses those props.";
  };
  WORLD_TRAVEL_OPENING_ONLY_REFERENCES.forEach((label) => {
    const option = document.createElement("label");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = worldTravelOmissionsForShot(shot, index).includes(label);
    checkbox.setAttribute("aria-label", `Omit ${label} from Shot ${index + 1}`);
    const copy = document.createElement("span");
    copy.textContent = `Omit ${label}`;
    checkbox.addEventListener("change", () => {
      const selected = new Set(worldTravelOmissionsForShot(shot, index));
      if (checkbox.checked) selected.add(label);
      else selected.delete(label);
      shot.omitSharedImageLabels = WORLD_TRAVEL_OPENING_ONLY_REFERENCES.filter((item) => selected.has(item));
      updateCopy();
      updateSeriesReview();
      saveSeriesDraftSoon();
    });
    option.append(checkbox, copy);
    controls.append(option);
  });
  updateCopy();
  editor.append(controls);

  const guard = document.createElement("p");
  guard.className = "shot-reference-policy-guard";
  guard.textContent = "Checked files are removed, not just discouraged in the prompt. Robot and all three travelers always remain; authored P8/P9 tags remap automatically.";
  editor.append(guard);
  return editor;
}

function renderSeriesShots() {
  elements.seriesShotList.replaceChildren();
  state.series.shots.forEach((shot, index) => {
    const card = document.createElement("article");
    card.className = "shot-card";
    card.dataset.shotId = shot.id;

    const header = document.createElement("div");
    header.className = "shot-card-header";
    const number = document.createElement("span");
    number.className = "shot-number";
    number.textContent = String(index + 1).padStart(2, "0");
    const title = document.createElement("input");
    title.type = "text";
    title.className = "shot-name-input";
    title.maxLength = 120;
    title.value = shot.title;
    title.setAttribute("aria-label", `Shot ${index + 1} title`);
    title.addEventListener("input", () => updateShotState(shot.id, "title", title.value));
    const actions = document.createElement("div");
    actions.className = "shot-actions";
    actions.append(
      shotActionButton(`Move shot ${index + 1} earlier`, "↑", () => moveSeriesShot(index, -1), { disabled: index === 0 }),
      shotActionButton(`Move shot ${index + 1} later`, "↓", () => moveSeriesShot(index, 1), { disabled: index === state.series.shots.length - 1 }),
      shotActionButton(`Duplicate shot ${index + 1}`, "⧉", () => duplicateSeriesShot(index), { disabled: state.series.shots.length >= 12 }),
      shotActionButton(`Delete shot ${index + 1}`, "×", () => deleteSeriesShot(index), { disabled: state.series.shots.length <= 2, className: "delete" }),
    );
    header.append(number, title, actions);

    const body = document.createElement("div");
    body.className = "shot-card-body";
    const promptLabel = document.createElement("label");
    promptLabel.className = "shot-prompt-label";
    promptLabel.setAttribute("for", `series-shot-prompt-${shot.id}`);
    promptLabel.append(document.createTextNode("Picture, movement, dialogue & sound"));
    const promptCount = document.createElement("span");
    promptCount.className = "shot-prompt-count";
    promptCount.dataset.shotPromptCount = String(index);
    promptCount.textContent = `${shot.prompt.length.toLocaleString()} typed`;
    promptLabel.append(promptCount);
    const prompt = document.createElement("textarea");
    prompt.id = `series-shot-prompt-${shot.id}`;
    prompt.maxLength = 10000;
    prompt.rows = 5;
    prompt.value = shot.prompt;
    prompt.placeholder = "What happens in this shot? Include timing, framing, action, exact dialogue, ambience, and a clear ending.";
    prompt.addEventListener("input", () => {
      promptCount.textContent = `${prompt.value.length.toLocaleString()} / 10,000`;
      updateShotState(shot.id, "prompt", prompt.value);
    });

    const referenceMap = document.createElement("details");
    referenceMap.className = "shot-reference-map";
    referenceMap.dataset.shotReferenceMap = String(index);
    const referenceSummary = document.createElement("summary");
    referenceSummary.textContent = isWorldTravel()
      ? "Effective H3 reference tags · authored P8/P9 remap automatically"
      : "Exact reference tags for this shot";
    const referenceList = document.createElement("div");
    referenceList.className = "shot-reference-map-list";
    referenceMap.append(referenceSummary, referenceList);

    const settings = document.createElement("div");
    settings.className = "shot-card-settings";
    const durationLabel = document.createElement("label");
    durationLabel.className = "shot-duration-control";
    const durationText = document.createElement("span");
    durationText.textContent = "Duration";
    const duration = document.createElement("input");
    duration.type = "range";
    duration.min = "5";
    duration.max = "15";
    duration.step = "0.5";
    duration.value = String(shot.duration);
    duration.setAttribute("aria-label", `Shot ${index + 1} requested duration`);
    const durationOutput = document.createElement("output");
    const updateDurationCopy = () => {
      const requested = Number(duration.value);
      durationOutput.textContent = `${requested.toFixed(1)}s · ${(alignedFrames(requested) / 24).toFixed(2)}s`;
    };
    updateDurationCopy();
    duration.addEventListener("input", () => {
      updateDurationCopy();
      updateShotState(shot.id, "duration", Number(duration.value));
    });
    durationLabel.append(durationText, duration, durationOutput);

    const seedLabel = document.createElement("label");
    const seedText = document.createElement("span");
    seedText.textContent = "Seed";
    const seed = document.createElement("input");
    seed.type = "text";
    seed.inputMode = "numeric";
    seed.value = shot.seed;
    seed.pattern = "[0-9]+";
    seed.setAttribute("aria-label", `Shot ${index + 1} seed`);
    seed.addEventListener("input", () => updateShotState(shot.id, "seed", seed.value));
    seedLabel.append(seedText, seed);
    settings.append(durationLabel, seedLabel);
    if (isWorldTravel()) body.append(renderShotSceneReference(shot, index), renderShotReferencePolicy(shot, index));
    body.append(promptLabel, prompt, referenceMap, settings);

    if (index > 0) {
      const continuity = document.createElement("p");
      continuity.className = "shot-continuity-note";
      const seconds = Number(elements.seriesContinuity.value || 3);
      continuity.textContent = seconds
        ? `${isWorldTravel() ? "P9 is" : "Automatically receives"} the previous shot's exact final frame; its last ${seconds} seconds also carry movement and stereo sound.`
        : "Continuity handoff is currently off.";
      body.append(continuity);
    }
    card.append(header, body);
    elements.seriesShotList.append(card);
  });
  $("#addSeriesShot").disabled = state.series.shots.length >= 12;
  updateSeriesReview();
}

function addSeriesShot() {
  if (state.series.shots.length >= 12) return;
  const previous = state.series.shots.at(-1);
  let nextSeed = BigInt(/^[0-9]+$/.test(previous?.seed || "") ? previous.seed : "20260828") + 1n;
  if (nextSeed > MAX_SEED) nextSeed = 1n;
  state.series.shots.push({
    id: newLocalId(),
    title: `Shot ${state.series.shots.length + 1}`,
    prompt: isWorldTravel()
      ? `Treat <Picture 9> as the already-completed previous moment. Begin the next unseen action at a calm, natural pace. Use <Picture 8> only as this shot's destination, architecture, terrain, light, and atmosphere anchor. Advance the same motivated journey through one visible discovery, one concise historical or cultural connection, and natural Chinese dialogue. Preserve the four travelers, route logic, screen direction, time progression, wardrobe, voices, carried props, and native stereo sound. Do not copy any earlier episode's country, plot, actions, blocking, landmarks, palette, or composition. No subtitles, narration, tourist-checklist montage, or extra cast.`
      : `Continue the previous scene without replaying its completed action. Preserve all supplied identities, geography, screen direction, wardrobe, lighting logic, and audio space. Describe the next clear action, camera direction, exact dialogue, ambience, sound effects, and the final composition. No subtitles or interface text.`,
    duration: Number(previous?.duration) || 10,
    seed: nextSeed.toString(),
    sceneReference: null,
    omitSharedImageLabels: isWorldTravel() ? [...WORLD_TRAVEL_OPENING_ONLY_REFERENCES] : [],
  });
  renderSeriesShots();
  saveSeriesDraftSoon();
  elements.seriesShotList.lastElementChild?.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" });
}

function duplicateSeriesShot(index) {
  if (state.series.shots.length >= 12 || !state.series.shots[index]) return;
  const source = state.series.shots[index];
  let nextSeed = BigInt(/^[0-9]+$/.test(source.seed) ? source.seed : "1") + 1n;
  if (nextSeed > MAX_SEED) nextSeed = 1n;
  state.series.shots.splice(index + 1, 0, {
    ...source,
    id: newLocalId(),
    title: `${source.title} copy`,
    seed: nextSeed.toString(),
    sceneReference: source.sceneReference ? { ...source.sceneReference, metadata: { ...(source.sceneReference.metadata || {}) } } : null,
    omitSharedImageLabels: isWorldTravel() && index === 0
      ? [...WORLD_TRAVEL_OPENING_ONLY_REFERENCES]
      : [...(source.omitSharedImageLabels || [])],
  });
  renderSeriesShots();
  saveSeriesDraftSoon();
}

function deleteSeriesShot(index) {
  const shot = state.series.shots[index];
  if (!shot || state.series.shots.length <= 2) return;
  if (!confirm(`Remove “${shot.title || `Shot ${index + 1}`}” from this draft?`)) return;
  state.series.sceneUploadVersions.set(shot.id, (state.series.sceneUploadVersions.get(shot.id) || 0) + 1);
  state.series.shots.splice(index, 1);
  renderSeriesShots();
  saveSeriesDraftSoon();
}

function moveSeriesShot(index, direction) {
  const target = index + direction;
  if (target < 0 || target >= state.series.shots.length) return;
  const [shot] = state.series.shots.splice(index, 1);
  state.series.shots.splice(target, 0, shot);
  renderSeriesShots();
  saveSeriesDraftSoon();
  $(`[data-shot-id="${CSS.escape(shot.id)}"] .shot-name-input`)?.focus();
}

function seriesBlockingStatus(status) {
  return ["starting", "waiting", "queued", "running", "generating", "validating", "stitching", "pausing", "cancelling"].includes(status);
}

function seriesReferenceMapForShot(shotIndex) {
  const pictureLabels = [];
  if (isWorldTravel()) {
    const shot = state.series.shots[shotIndex];
    const omitted = new Set(worldTravelOmissionsForShot(shot, shotIndex));
    let physicalSlot = 0;
    state.series.canonical.slice(0, WORLD_TRAVEL_REFERENCES.length).forEach((asset, index) => {
      const label = WORLD_TRAVEL_REFERENCES[index][0];
      if (asset && !omitted.has(label)) pictureLabels.push([++physicalSlot, label, index + 1]);
    });
    if (shot?.sceneReference) pictureLabels.push([++physicalSlot, sceneReferenceLabel(shot, shotIndex), 8]);
    if (shotIndex > 0 && Number(elements.seriesContinuity.value)) {
      pictureLabels.push([++physicalSlot, "previous shot's exact final frame", 9]);
    }
  } else {
    const sequential = [];
    state.series.canonical.slice(0, seriesReferenceLabels().length).forEach((asset, index) => {
      if (asset) sequential.push(seriesReferenceLabels()[index][0]);
    });
    state.series.extras.images.forEach((asset) => sequential.push((asset.label || asset.name).trim()));
    sequential.forEach((label, index) => pictureLabels.push([index + 1, label]));
  }

  const videos = state.series.extras.videos.map((asset) => {
    const label = (asset.label || asset.name).trim();
    const audioLabel = asset.soundtrack ? `${label} soundtrack` : asset.metadata?.has_audio ? `original audio from ${label}` : null;
    return [label, audioLabel];
  });
  const continuitySeconds = Number(elements.seriesContinuity.value);
  if (shotIndex > 0 && continuitySeconds) {
    if (!isWorldTravel()) pictureLabels.push([pictureLabels.length + 1, "previous shot's exact final frame"]);
    videos.push([`previous shot's final ${continuitySeconds} seconds`, "stereo audio from the previous shot continuity tail"]);
  }

  const labels = pictureLabels.map(([ordinal, label, logicalOrdinal = ordinal]) => {
    const authored = logicalOrdinal === ordinal ? "" : ` · authored <Picture ${logicalOrdinal}>`;
    return `<Picture ${ordinal}> = ${label}${authored}`;
  });
  let audioIndex = 1;
  videos.forEach(([videoLabel, audioLabel], index) => {
    if (audioLabel) labels.push(`<Audio ${audioIndex++}> = ${audioLabel}`);
    labels.push(`<Video ${index + 1}> = ${videoLabel}`);
  });
  state.series.extras.audio.forEach((asset) => labels.push(`<Audio ${audioIndex++}> = ${(asset.label || asset.name).trim()}`));
  return labels;
}

function updateShotReferenceMaps() {
  for (const container of $$('[data-shot-reference-map]', elements.seriesShotList)) {
    const list = $(".shot-reference-map-list", container);
    if (!list) continue;
    const labels = seriesReferenceMapForShot(Number(container.dataset.shotReferenceMap));
    list.replaceChildren();
    if (!labels.length) {
      const empty = document.createElement("span");
      empty.textContent = "No tagged references in this shot yet.";
      list.append(empty);
      continue;
    }
    for (const label of labels) {
      const code = document.createElement("code");
      code.textContent = label;
      list.append(code);
    }
  }
  for (const counter of $$('[data-shot-prompt-count]', elements.seriesShotList)) {
    const index = Number(counter.dataset.shotPromptCount);
    const shot = state.series.shots[index];
    if (!shot) continue;
    counter.textContent = `${shot.prompt.length.toLocaleString()} typed · ${composedSeriesPromptLength(shot, index).toLocaleString()} / 12,000 final`;
  }
}

function composedSeriesPromptLength(shot, shotIndex) {
  let guidance;
  if (state.series.template === "lalachan") {
    guidance = "LALACHAN series continuity: keep every named character's species, human face, costume, scale, voice and relationships exact across shots. Use natural Chinese dialogue and clear screen direction; do not add, merge, duplicate or replace cast members.";
  } else if (isWorldTravel()) {
    const pictureGuidance = worldTravelOmissionsForShot(shot, shotIndex).length
      ? "Shared pictures present in the effective map are identity and series-style anchors only. The shot-specific location picture controls location, architecture, terrain, light and atmosphere for this shot only."
      : "Pictures 1-7 are shared identity and series-style anchors only. Picture 8 controls location, architecture, terrain, light and atmosphere for this shot only.";
    guidance = `LALACHAN World Travel continuity: lock every named character's identity, species, human face, body scale, wardrobe, accessories, relationships and voice across the whole journey. Keep the stated travel route, geography, screen direction and time progression coherent. ${pictureGuidance} Do not carry its place-specific details into another destination. Any earlier-episode, video or audio reference may guide character appearance or voice timbre only: never copy its country, plot, story direction, actions, blocking, landmarks or visual composition. Use natural concise dialogue and let one motivated journey connect the history and sights instead of presenting an unrelated tourist checklist.`;
  } else {
    guidance = "Movie continuity: preserve cast identity, wardrobe, props, geography, lighting direction, voices, action direction and the state at the previous shot's final frame.";
  }
  let continuity = "";
  if (shotIndex > 0 && Number(elements.seriesContinuity.value)) {
    continuity = " The previous-shot reference tail is context only; treat its exact final frame as time zero. Frame 1 begins the next unseen moment, and the new action continues at a calm, natural human pace. Never replay, extend, pause on or restart the completed outgoing movement.";
    if (isWorldTravel()) {
      continuity += " Complete any needed location match within 0.5 seconds without accelerating people; after that, show only the current location and this shot's new action.";
    }
  }
  const resolution = elements.seriesResolution.selectedOptions[0];
  const target = `\nTarget output: ${Number(resolution?.dataset.width)}x${Number(resolution?.dataset.height)}; ${Number(shot.duration)} seconds.`;
  const briefText = elements.seriesBrief.value.trim();
  const brief = briefText ? `\nSeries note: ${briefText}` : "";
  const labels = seriesReferenceMapForShot(shotIndex);
  const referenceMap = labels.length ? `\nReference map:\n${labels.join("\n")}` : "";
  return `Series: ${elements.seriesTitle.value.trim()}. Shot ${shotIndex + 1} of ${state.series.shots.length}: ${shot.title.trim()}.\n${guidance}${continuity}${target}${brief}${referenceMap}\n\n${shot.prompt.trim()}`.length;
}

function unresolvedSeriesPromptTags() {
  const unresolved = [];
  const continuitySeconds = Number(elements.seriesContinuity.value);
  const sharedPictures = seriesSharedImageCount();
  const sharedVideos = state.series.extras.videos.length;
  const sharedAudio = state.series.extras.audio.length + state.series.extras.videos.filter((asset) => asset.soundtrack || asset.metadata?.has_audio).length;
  state.series.shots.forEach((shot, shotIndex) => {
    const hasContinuity = shotIndex > 0 && continuitySeconds > 0;
    const limits = {
      picture: sharedPictures + (hasContinuity ? 1 : 0),
      video: sharedVideos + (hasContinuity ? 1 : 0),
      audio: sharedAudio + (hasContinuity ? 1 : 0),
    };
    const worldPictures = new Set();
    if (isWorldTravel()) {
      const omitted = new Set(worldTravelOmissionsForShot(shot, shotIndex));
      state.series.canonical.slice(0, WORLD_TRAVEL_REFERENCES.length).forEach((asset, index) => {
        if (asset && !omitted.has(WORLD_TRAVEL_REFERENCES[index][0])) worldPictures.add(index + 1);
      });
      if (shot.sceneReference) worldPictures.add(8);
      if (hasContinuity) worldPictures.add(9);
    }
    const text = `${elements.seriesBrief.value}\n${shot.prompt}`;
    for (const match of text.matchAll(/<(Picture|Video|Audio)\s+([0-9]+)>/gi)) {
      const kind = match[1].toLowerCase();
      const ordinal = Number(match[2]);
      const resolved = kind === "picture" && isWorldTravel()
        ? worldPictures.has(ordinal)
        : ordinal >= 1 && ordinal <= limits[kind];
      if (!resolved) unresolved.push(`Shot ${shotIndex + 1}: <${match[1]} ${match[2]}>`);
    }
  });
  return [...new Set(unresolved)];
}

function unstableSeriesAudioTags() {
  const semantics = new Map();
  state.series.shots.forEach((shot, shotIndex) => {
    const labels = new Map();
    for (const label of seriesReferenceMapForShot(shotIndex)) {
      const match = label.match(/^<Audio\s+([0-9]+)>\s*=\s*(.+)$/i);
      if (match) labels.set(Number(match[1]), match[2]);
    }
    const text = `${elements.seriesBrief.value}\n${shot.prompt}`;
    for (const match of text.matchAll(/<Audio\s+([0-9]+)>/gi)) {
      const ordinal = Number(match[1]);
      const label = labels.get(ordinal);
      if (!label) continue;
      if (!semantics.has(ordinal)) semantics.set(ordinal, new Set());
      semantics.get(ordinal).add(label);
    }
  });
  return [...semantics.entries()].filter(([, labels]) => labels.size > 1).map(([ordinal]) => `<Audio ${ordinal}>`);
}

function seriesDraftChecks() {
  const checks = [];
  const add = (kind, title, detail) => checks.push({ kind, title, detail });
  const title = elements.seriesTitle.value.trim();
  add(title ? "ok" : "error", title ? "Project named" : "Add a project title", title || "A short working title makes the saved series easy to find.");

  const invalidShot = state.series.shots.findIndex((shot) => {
    const seed = String(shot.seed).trim();
    let validSeed = /^[0-9]+$/.test(seed);
    if (validSeed) {
      try { validSeed = BigInt(seed) <= MAX_SEED; } catch { validSeed = false; }
    }
    return !shot.title.trim() || !shot.prompt.trim() || shot.prompt.length > 10000 || shot.duration < 5 || shot.duration > 15 || !validSeed;
  });
  add(invalidShot < 0 && state.series.shots.length >= 2 && state.series.shots.length <= 12 ? "ok" : "error",
    invalidShot < 0 ? `${state.series.shots.length} shots are directed` : `Fix Shot ${invalidShot + 1}`,
    invalidShot < 0 ? "Every shot has a title, prompt, aligned duration, and valid seed." : "Each shot needs a title, prompt, 5–15 second duration, and unsigned 64-bit seed.");

  const overBudgetShot = state.series.shots.findIndex((shot, index) => composedSeriesPromptLength(shot, index) > 12000);
  if (overBudgetShot >= 0) {
    add("error", `Shot ${overBudgetShot + 1} exceeds the final prompt budget`, "Shorten its direction or the global Story note. The 12,000-character check includes continuity guidance and the reference map.");
  }

  const unresolvedTags = unresolvedSeriesPromptTags();
  if (unresolvedTags.length) {
    add("error", "Some prompt tags have no effective reference", `${unresolvedTags.slice(0, 3).join(" · ")}${unresolvedTags.length > 3 ? ` · plus ${unresolvedTags.length - 3} more` : ""}. Upload the match, keep an intentionally used opening reference, or edit the tag before GPU work starts.`);
  }
  const unstableAudio = unstableSeriesAudioTags();
  if (unstableAudio.length) {
    add("error", "An audio tag changes meaning between shots", `${unstableAudio.join(", ")} points to different sounds after the continuity tail is added. Use the reference's name in shared direction, or use each shot card's exact tag map.`);
  }

  const canonicalReady = state.series.canonical.slice(0, seriesReferenceLabels().length).filter(Boolean).length;
  const sharedImageCount = seriesSharedImageCount();
  if (isWorldTravel()) {
    add(canonicalReady === WORLD_TRAVEL_REFERENCES.length ? "ok" : "error",
      canonicalReady === WORLD_TRAVEL_REFERENCES.length ? "World Travel identity set is complete" : `${WORLD_TRAVEL_REFERENCES.length - canonicalReady} shared P1–P7 reference${WORLD_TRAVEL_REFERENCES.length - canonicalReady === 1 ? " is" : "s are"} missing`,
      "The seven canonical slots keep characters, recurring props, and series style stable without steering the destination story.");
    const laterPolicies = state.series.shots.slice(1).map((shot, offset) => worldTravelOmissionsForShot(shot, offset + 1));
    const fullyScoped = laterPolicies.filter((labels) => labels.length === WORLD_TRAVEL_OPENING_ONLY_REFERENCES.length).length;
    const keptOpeningRefs = laterPolicies.length - fullyScoped;
    add(keptOpeningRefs ? "warn" : "ok",
      keptOpeningRefs ? `${keptOpeningRefs} later shot${keptOpeningRefs === 1 ? " keeps" : "s keep"} an opening-only picture` : "Later shots exclude opening-only pictures",
      keptOpeningRefs
        ? "This is allowed for deliberate prop use. Check each shot's effective H3 map so the words card, glasses, or notebook cannot pull a later scene back toward the opening."
        : "Words card, LightMind glasses, and Patchwork notebook are removed after Shot 1; Robot and all three travelers remain in every shot.");
    const sceneReady = seriesReadySceneCount();
    add(sceneReady === state.series.shots.length ? "ok" : "error",
      sceneReady === state.series.shots.length ? `All ${sceneReady} shot-specific P8 plates are ready` : `${state.series.shots.length - sceneReady} shot-specific P8 plate${state.series.shots.length - sceneReady === 1 ? " is" : "s are"} missing`,
      "Every shot needs its own location, architecture, terrain, light, and atmosphere image; reorder and duplicate operations keep the plate attached to its shot.");
    const continuityReady = Number(elements.seriesContinuity.value) > 0;
    add(continuityReady ? "ok" : "error",
      continuityReady ? "P9 continuity is reserved" : "Turn on World Travel continuity",
      continuityReady ? "From Shot 2 onward, P9 is always the previous shot's exact final frame." : "Choose a 2–4 second handoff so the previous final frame has the stable P9 position.");
    if (state.series.extras.images.length) {
      add("warn", `${state.series.extras.images.length} extra shared picture${state.series.extras.images.length === 1 ? " is" : "s are"} parked`, "They are preserved for LALACHAN Series and My Movie but are not sent here, so the scene plate stays P8 and continuity stays P9.");
    }
  } else {
    add(sharedImageCount <= 8 ? "ok" : "error",
      sharedImageCount <= 8 ? `${sharedImageCount} of 8 shared picture slots used` : `${sharedImageCount - 8} too many shared pictures`,
      sharedImageCount <= 8
        ? "Picture 9 remains reserved for the previous shot's final frame."
        : "Remove extra pictures until no more than eight shared images remain; uploads are preserved until you choose which handles to remove.");
  }
  if (state.series.template === "lalachan") {
    add(canonicalReady === LALACHAN_REFERENCES.length ? "ok" : "error",
      canonicalReady === LALACHAN_REFERENCES.length ? "LALACHAN cast is complete" : `${LALACHAN_REFERENCES.length - canonicalReady} LALACHAN reference${LALACHAN_REFERENCES.length - canonicalReady === 1 ? " is" : "s are"} missing`,
      "All seven fixed picture positions keep names and prompt tags stable across the series.");
  } else if (state.series.template === "movie") {
    const movieSlots = state.series.canonical.slice(0, MOVIE_REFERENCES.length);
    const firstGap = movieSlots.findIndex((asset) => !asset);
    const hasNumberingGap = firstGap >= 0 && movieSlots.slice(firstGap + 1).some(Boolean);
    if (hasNumberingGap) add("error", "Movie picture slots must stay consecutive", `Fill P${firstGap + 1} or remove the later canonical pictures so prompt tags cannot silently change identity.`);
    add(seriesReadyAssets() ? "ok" : "warn", seriesReadyAssets() ? "Shared references ready" : "Blank movie mode", seriesReadyAssets() ? "The same cast and world references will reach every shot." : "You can generate without a cast, but one strong picture usually improves continuity.");
  }

  const engineReady = state.health?.connected && state.health?.ready !== false;
  add(engineReady ? "ok" : "error", engineReady ? "Local H3 engine is ready" : "Local H3 engine needs attention", engineReady ? "Shots will run sequentially; no second heavy render is launched." : state.health?.message || "Start the verified local runtime before generating.");
  const profile = state.config?.profiles.find((item) => item.id === elements.seriesProfile.value);
  if (profile?.dual_gpu && deviceCount() < 2) add("error", "Maximum quality needs both GPUs", "Choose a single-GPU profile or make both RTX 4090 devices available.");
  if (state.series.uploading) add("error", "Uploads are still processing", `Wait for ${state.series.uploading} reference upload${state.series.uploading === 1 ? "" : "s"} to finish.`);
  if (state.series.assetValidation !== "verified") {
    add(
      "error",
      state.series.assetValidation === "checking" ? "Saved references are being checked" : "Saved references could not be verified",
      state.series.assetValidation === "checking" ? "Wait for the upload-handle check to finish." : "Reload to retry the check, or upload the references again before generating.",
    );
  }
  const active = state.series.record && seriesBlockingStatus(state.series.record.status);
  if (active) add("error", "A series is already running", "Pause or finish the current series before starting another one.");
  return checks;
}

function renderPreflight(checks) {
  elements.seriesPreflight.replaceChildren();
  for (const check of checks) {
    const row = document.createElement("div");
    row.className = `preflight-item${check.kind === "ok" ? "" : ` ${check.kind}`}`;
    const icon = document.createElement("span");
    icon.className = "preflight-icon";
    icon.textContent = check.kind === "ok" ? "✓" : check.kind === "warn" ? "!" : "×";
    icon.setAttribute("aria-hidden", "true");
    const copy = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = check.title;
    const small = document.createElement("small");
    small.textContent = check.detail;
    copy.append(strong, small);
    row.append(icon, copy);
    elements.seriesPreflight.append(row);
  }
}

function updateDraftTimeline() {
  if (state.series.record) return;
  elements.seriesTimeline.replaceChildren();
  state.series.shots.forEach((shot, index) => {
    const item = document.createElement("li");
    item.className = "timeline-shot queued";
    const marker = document.createElement("span");
    marker.className = "timeline-marker";
    marker.textContent = String(index + 1).padStart(2, "0");
    const card = document.createElement("div");
    card.className = "timeline-card";
    const heading = document.createElement("div");
    heading.className = "timeline-card-heading";
    const copy = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = shot.title || `Shot ${index + 1}`;
    const small = document.createElement("small");
    small.textContent = `${actualShotSeconds(shot).toFixed(2)} s actual · seed ${shot.seed || "—"} · ready to queue`;
    copy.append(strong, small);
    heading.append(copy);
    card.append(heading);
    item.append(marker, card);
    elements.seriesTimeline.append(item);
  });
}

function updateSeriesReview() {
  if (!state.config || !state.series.shots.length) return;
  updateShotReferenceMaps();
  const totals = seriesTotals();
  const shotWord = state.series.shots.length === 1 ? "shot" : "shots";
  elements.seriesShotCount.textContent = `${state.series.shots.length} ${shotWord}`;
  elements.seriesDurationTotal.textContent = `${totals.seconds.toFixed(2)} s actual · ${totals.frames} frames`;
  elements.seriesStartSummary.textContent = `${state.series.shots.length} ${shotWord} · ${totals.seconds.toFixed(2)} s · one at a time`;
  elements.seriesSummaryTitle.textContent = elements.seriesTitle.value.trim() || "Untitled series";
  if (!state.series.record) {
    elements.seriesStatusBadge.textContent = "Draft";
    elements.seriesStatusBadge.className = "series-status-badge draft";
    elements.seriesSummaryText.textContent = state.series.template === "lalachan"
      ? "Seven stable cast slots, editable shot cards, and an automatic continuity handoff between scenes."
      : isWorldTravel()
        ? "Seven shared identity anchors, one destination plate per shot, and stable P8/P9 handoffs across the journey."
        : "A flexible local storyboard for your own cast, world, and movie.";
    renderSeriesMetrics([
      ["Template", seriesTemplateLabel()],
      ["Timeline", `${state.series.shots.length} shots`],
      ["Actual length", `${totals.seconds.toFixed(2)} s`],
      [isWorldTravel() ? "P8 scene plates" : "Shared assets", String(isWorldTravel() ? seriesReadySceneCount() : seriesReadyAssets())],
    ]);
    updateDraftTimeline();
  }
  const checks = seriesDraftChecks();
  renderPreflight(checks);
  elements.startSeries.disabled = checks.some((check) => check.kind === "error");
}

function renderSeriesMetrics(items) {
  elements.seriesSummaryMetrics.replaceChildren();
  for (const [label, value] of items) {
    const metric = document.createElement("div");
    metric.className = "series-summary-metric";
    const key = document.createElement("span");
    key.textContent = label;
    const strong = document.createElement("strong");
    strong.textContent = value;
    metric.append(key, strong);
    elements.seriesSummaryMetrics.append(metric);
  }
}

function seriesPayload() {
  const resolution = elements.seriesResolution.selectedOptions[0];
  const images = [];
  state.series.canonical.slice(0, seriesReferenceLabels().length).forEach((asset, index) => {
    if (asset) images.push({ token: asset.token, label: seriesReferenceLabels()[index][0] });
  });
  if (!isWorldTravel()) state.series.extras.images.forEach((asset) => images.push({ token: asset.token, label: asset.label || asset.name }));
  return {
    title: elements.seriesTitle.value.trim(),
    brief: elements.seriesBrief.value.trim(),
    template: state.series.template,
    settings: {
      profile: elements.seriesProfile.value,
      width: Number(resolution?.dataset.width),
      height: Number(resolution?.dataset.height),
      ref_image_size: elements.seriesRefImageSize.value,
      continuity_seconds: Number(elements.seriesContinuity.value),
      advance: true,
    },
    references: {
      images,
      videos: state.series.extras.videos.map((asset) => ({ token: asset.token, label: asset.label || asset.name, soundtrack: asset.soundtrack?.token || null })),
      audio: state.series.extras.audio.map((asset) => ({ token: asset.token, label: asset.label || asset.name })),
    },
    shots: state.series.shots.map((shot, index) => ({
      title: shot.title.trim(),
      prompt: shot.prompt.trim(),
      duration: Number(shot.duration),
      seed: String(shot.seed).trim(),
      ...(isWorldTravel() ? {
        scene_reference: {
          token: shot.sceneReference?.token || "",
          label: sceneReferenceLabel(shot, index),
        },
        omit_shared_image_labels: worldTravelOmissionsForShot(shot, index),
      } : {}),
    })),
  };
}

function unwrapSeries(result) {
  if (result?.series && typeof result.series === "object" && !Array.isArray(result.series)) return result.series;
  return result;
}

async function submitSeries(event) {
  event.preventDefault();
  const checks = seriesDraftChecks();
  renderPreflight(checks);
  if (checks.some((check) => check.kind === "error")) {
    setSeriesMessage("Finish the highlighted preflight items first.", "error");
    elements.seriesPreflight.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  elements.startSeries.disabled = true;
  setSeriesMessage("Saving the storyboard and checking every reference…");
  const payload = seriesPayload();
  try {
    let saved;
    if (state.series.serverDraftId) {
      try {
        saved = unwrapSeries(await api(`/api/series/${encodeURIComponent(state.series.serverDraftId)}`, { method: "PUT", body: JSON.stringify(payload) }));
      } catch (error) {
        if (error.status !== 404) throw error;
        state.series.serverDraftId = null;
      }
    }
    if (!saved) saved = unwrapSeries(await api("/api/series", { method: "POST", body: JSON.stringify(payload) }));
    if (!saved?.id) throw new Error("The server saved the series but did not return its id.");
    state.series.serverDraftId = saved.id;
    state.series.record = saved;
    renderSeriesRecord(saved);
    setSeriesMessage("Storyboard saved. Starting Shot 1…");
    const started = unwrapSeries(await api(`/api/series/${encodeURIComponent(saved.id)}/start`, { method: "POST", body: "{}" }));
    state.series.record = started?.id ? started : saved;
    state.series.serverDraftId = null;
    saveSeriesDraftSoon();
    renderSeriesRecord(state.series.record);
    setSeriesMessage("Series started. H3 will save and validate one shot at a time.", "success");
    startSeriesPolling();
    await refreshSeriesLibrary();
    elements.seriesOutputWorkspace.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  } catch (error) {
    setSeriesMessage(error.message, "error");
    if (state.series.record) renderSeriesRecord(state.series.record);
  } finally {
    updateSeriesReview();
  }
}

function seriesShotDuration(shot) {
  if (Number.isFinite(Number(shot.actual_duration))) return Number(shot.actual_duration);
  if (Number.isFinite(Number(shot.length))) return Number(shot.length) / 24;
  return actualShotSeconds({ duration: Number(shot.duration) || 5 });
}

function safeStatus(value, fallback = "draft") {
  const status = String(value || fallback).toLowerCase().replace(/[^a-z0-9_-]/g, "");
  return status || fallback;
}

function seriesStatusCopy(record) {
  const status = safeStatus(record.status);
  const activeIndex = Number.isInteger(record.active_shot) ? record.active_shot + 1 : null;
  const copy = {
    draft: "Saved locally and ready to start.",
    ready: "This durable saved project is ready. Start it here; the storyboard editor on the left remains a separate new browser draft.",
    starting: "Preparing the first native H3 graph.",
    waiting: "Another local render is active. This series will begin automatically after it finishes—no second heavy job is launched.",
    queued: activeIndex ? `Shot ${activeIndex} is waiting for the local engine.` : "The first shot is queued.",
    running: activeIndex ? `Shot ${activeIndex} is generating. Later shots stay safely queued.` : "The series is running one shot at a time.",
    generating: activeIndex ? `Shot ${activeIndex} is generating. Later shots stay safely queued.` : "H3 is generating the active shot.",
    validating: activeIndex ? `Shot ${activeIndex} is saved and being validated.` : "The latest shot is being validated before continuity handoff.",
    pausing: "The current shot will finish and save before the series pauses.",
    paused: "Paused safely between shots. Resume whenever the workstation is ready.",
    stitching: "All shots are safe; H3 Studio is assembling the final movie.",
    completed: "The stitched movie and every individual shot are ready.",
    failed: "Generation stopped safely. Completed shots and all attempts remain available.",
    cancelled: "The active shot was cancelled. Earlier shots and attempts remain saved.",
  };
  return copy[status] || "The local series record is up to date.";
}

function recordReferenceCount(record) {
  const references = record.references || {};
  if (Array.isArray(references)) return references.length;
  return [references.images, references.videos, references.audio].reduce((sum, list) => sum + (Array.isArray(list) ? list.length : 0), 0);
}

function renderSeriesRecord(record) {
  if (!record || typeof record !== "object") return;
  state.series.record = record;
  const status = safeStatus(record.status);
  const shots = Array.isArray(record.shots) ? record.shots : [];
  const ready = shots.filter((shot) => ["ready", "completed"].includes(safeStatus(shot.status))).length;
  const seconds = shots.reduce((sum, shot) => sum + seriesShotDuration(shot), 0);
  elements.seriesStatusBadge.textContent = status.replaceAll("_", " ");
  elements.seriesStatusBadge.className = `series-status-badge ${status}`;
  elements.seriesSummaryTitle.textContent = record.title || "Untitled series";
  elements.seriesSummaryText.textContent = seriesStatusCopy(record);
  const recordError = status === "failed" && record.error != null ? String(record.error).trim().slice(0, 4000) : "";
  elements.seriesRecordError.hidden = !recordError;
  elements.seriesRecordError.textContent = recordError ? `Series-level failure: ${recordError}` : "";
  renderSeriesMetrics([
    ["Progress", `${ready} / ${shots.length} ready`],
    ["Actual length", `${seconds.toFixed(2)} s`],
    ["Active shot", Number.isInteger(record.active_shot) ? `${record.active_shot + 1} of ${shots.length}` : "—"],
    ["Shared assets", String(recordReferenceCount(record))],
  ]);

  const canStartSaved = status === "ready";
  const canPause = ["queued", "waiting", "running"].includes(status);
  const pausePending = status === "pausing";
  const canResume = status === "paused";
  const canCancelActive = ["queued", "waiting", "running", "pausing"].includes(status) && Number.isInteger(record.active_shot);
  const canRetryFinalization = status === "failed" && record.active_shot == null && shots.length > 0
    && shots.every((shot) => safeStatus(shot.status) === "completed" && shot.accepted_attempt != null);
  elements.seriesControls.hidden = !(canStartSaved || canPause || pausePending || canResume || canCancelActive || canRetryFinalization);
  elements.startSavedSeries.hidden = !canStartSaved;
  if (canStartSaved) elements.startSavedSeries.disabled = false;
  elements.pauseSeries.hidden = !(canPause || pausePending);
  elements.pauseSeries.disabled = pausePending;
  elements.pauseSeries.textContent = pausePending ? "Pause requested — finishing current shot" : "Pause after this shot";
  elements.resumeSeries.hidden = !canResume;
  elements.retrySeriesFinalization.hidden = !canRetryFinalization;
  if (canRetryFinalization) elements.retrySeriesFinalization.disabled = false;
  elements.cancelSeriesActive.hidden = !canCancelActive;
  renderSeriesTimeline(record);
  renderSeriesFinal(record);
  updateSeriesReview();
}

function attemptArtifacts(attempt) {
  return Array.isArray(attempt?.outputs) ? attempt.outputs.filter((artifact) => artifact && typeof artifact === "object") : [];
}

function playableArtifact(artifacts) {
  return artifacts.find((artifact) => artifact.kind === "video" || artifact.kind === "shot" || /\.mp4(?:$|\?)/i.test(artifact.url || "")) || null;
}

function displayShotStatus(rawStatus, index, activeShot) {
  const status = safeStatus(rawStatus, "pending");
  if (status === "completed") return "ready";
  if (["in_progress", "rendering"].includes(status)) return "generating";
  if (["submitting", "cancelling"].includes(status)) return "active";
  if (["pending", "queued"].includes(status)) return index === activeShot ? "active" : "queued";
  return status;
}

function appendAttemptDisclosure(card, shot) {
  const attempts = Array.isArray(shot.attempts) ? shot.attempts : [];
  if (!attempts.length) return;
  const details = document.createElement("details");
  details.className = "attempt-disclosure";
  if (["failed", "cancelled"].includes(safeStatus(shot.status))) details.open = true;
  const summary = document.createElement("summary");
  summary.textContent = `${attempts.length} preserved attempt${attempts.length === 1 ? "" : "s"} · show all`;
  const list = document.createElement("div");
  list.className = "attempt-list";
  attempts.forEach((attempt, index) => {
    const row = document.createElement("div");
    row.className = "attempt-row";
    const label = document.createElement("span");
    const number = Number.isFinite(Number(attempt.number)) ? Number(attempt.number) : index + 1;
    const accepted = Number(shot.accepted_attempt) === number;
    label.textContent = `Attempt ${number}${accepted ? " · accepted" : ""}`;
    const status = document.createElement("span");
    status.textContent = safeStatus(attempt.status).replaceAll("_", " ");
    row.append(label, status);
    const artifacts = attemptArtifacts(attempt);
    const downloadable = artifacts.find((artifact) => artifact.download_url || artifact.url);
    if (downloadable) {
      const link = document.createElement("a");
      link.href = downloadable.download_url || downloadable.url;
      link.download = "";
      link.textContent = "Download";
      link.setAttribute("aria-label", `Download Shot ${Number(shot.index) + 1} attempt ${number}`);
      row.append(link);
    } else {
      const spacer = document.createElement("span");
      spacer.textContent = "—";
      row.append(spacer);
    }
    if (attempt.error) {
      const error = document.createElement("span");
      error.className = "attempt-error";
      error.textContent = String(attempt.error);
      row.append(error);
    }
    list.append(row);
  });
  details.append(summary, list);
  card.append(details);
}

function renderSeriesTimeline(record) {
  elements.seriesTimeline.replaceChildren();
  const shots = Array.isArray(record.shots) ? record.shots : [];
  if (!shots.length) {
    const empty = document.createElement("li");
    empty.className = "timeline-empty";
    empty.textContent = "This series does not have any saved shots yet.";
    elements.seriesTimeline.append(empty);
    return;
  }
  const seriesStatus = safeStatus(record.status);
  const actionsAllowed = ["paused", "failed", "cancelled", "completed"].includes(seriesStatus);
  shots.forEach((shot, position) => {
    const index = Number.isInteger(shot.index) ? shot.index : position;
    const status = displayShotStatus(shot.status, index, record.active_shot);
    const item = document.createElement("li");
    item.className = `timeline-shot ${status}`;
    const marker = document.createElement("span");
    marker.className = "timeline-marker";
    marker.textContent = ["ready", "completed"].includes(status) ? "✓" : String(index + 1).padStart(2, "0");
    const card = document.createElement("div");
    card.className = "timeline-card";
    const heading = document.createElement("div");
    heading.className = "timeline-card-heading";
    const copy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = shot.title || `Shot ${index + 1}`;
    const detail = document.createElement("small");
    const statusText = status === "active" ? "active · waiting or generating" : status.replaceAll("_", " ");
    detail.textContent = `${statusText} · ${seriesShotDuration(shot).toFixed(2)} s actual · seed ${shot.seed || "—"}`;
    copy.append(title, detail);
    heading.append(copy);
    card.append(heading);

    const percent = index < record.active_shot || ["ready", "completed"].includes(status)
      ? 100
      : index === record.active_shot && Number.isFinite(Number(record.progress?.percent)) ? Math.max(2, Math.min(100, Number(record.progress.percent))) : 0;
    if (percent || ["active", "generating", "validating"].includes(status)) {
      const track = document.createElement("div");
      track.className = "timeline-progress";
      const fill = document.createElement("span");
      fill.style.width = `${percent || 7}%`;
      track.append(fill);
      card.append(track);
    }

    const attempts = Array.isArray(shot.attempts) ? shot.attempts : [];
    const acceptedNumber = Number(shot.accepted_attempt);
    const accepted = attempts.find((attempt) => Number(attempt.number) === acceptedNumber)
      || [...attempts].reverse().find((attempt) => attemptArtifacts(attempt).length);
    const artifacts = attemptArtifacts(accepted);
    const playable = playableArtifact(artifacts);
    if (playable || actionsAllowed) {
      const media = document.createElement("div");
      media.className = "timeline-media";
      if (playable?.url) {
        const video = document.createElement("video");
        video.controls = true;
        video.playsInline = true;
        video.preload = "metadata";
        video.src = playable.url;
        video.setAttribute("aria-label", `Shot ${index + 1} accepted video`);
        media.append(video);
      }
      const actions = document.createElement("div");
      actions.className = "timeline-actions";
      if (playable?.download_url || playable?.url) {
        const download = document.createElement("a");
        download.className = "secondary-button";
        download.href = playable.download_url || playable.url;
        download.download = "";
        download.textContent = "Download shot";
        actions.append(download);
      }
      if (actionsAllowed && ["failed", "cancelled"].includes(status)) {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "secondary-button";
        retry.textContent = "Retry shot";
        retry.addEventListener("click", () => retrySeriesShot(record.id, index, false));
        actions.append(retry);
      }
      if (actionsAllowed && ["ready", "completed"].includes(status)) {
        const regenerate = document.createElement("button");
        regenerate.type = "button";
        regenerate.className = "text-button";
        regenerate.textContent = position < shots.length - 1 ? "Regenerate from here" : "Regenerate this shot";
        regenerate.title = "Old attempts stay saved; later continuity is rebuilt.";
        regenerate.addEventListener("click", () => retrySeriesShot(record.id, index, true));
        actions.append(regenerate);
      }
      if (actions.childElementCount) media.append(actions);
      if (media.childElementCount) card.append(media);
    }
    appendAttemptDisclosure(card, { ...shot, index });
    item.append(marker, card);
    elements.seriesTimeline.append(item);
  });
}

function renderSeriesFinal(record) {
  const artifacts = Array.isArray(record.artifacts) ? record.artifacts : [];
  const matchingPreferred = record.final_artifact?.id
    ? artifacts.find((artifact) => artifact?.id === record.final_artifact.id)
    : null;
  const preferred = record.final_artifact && record.final_artifact.kind === "final"
    && record.final_artifact.superseded !== true && matchingPreferred?.superseded !== true
    ? record.final_artifact
    : null;
  const final = preferred || artifacts.find((artifact) => artifact?.kind === "final" && artifact.superseded !== true) || null;
  elements.seriesFinalPanel.hidden = !final?.url;
  if (!final?.url) {
    elements.seriesFinalVideo.pause();
    elements.seriesFinalVideo.removeAttribute("src");
    delete elements.seriesFinalVideo.dataset.url;
    elements.seriesFinalVideo.load();
    return;
  }
  if (elements.seriesFinalVideo.dataset.url !== final.url) {
    elements.seriesFinalVideo.dataset.url = final.url;
    elements.seriesFinalVideo.src = final.url;
    elements.seriesFinalVideo.load();
  }
  elements.downloadSeriesFinal.href = final.download_url || final.url;
}

async function refreshActiveSeries() {
  const id = state.series.record?.id;
  if (!id) {
    updateSeriesReview();
    return;
  }
  try {
    const record = unwrapSeries(await api(`/api/series/${encodeURIComponent(id)}`));
    renderSeriesRecord(record);
    const status = safeStatus(record.status);
    if (seriesBlockingStatus(status) || status === "pausing") scheduleSeriesPoll(2200);
    else if (status === "paused") scheduleSeriesPoll(7000);
    else clearTimeout(state.series.pollTimer);
  } catch (error) {
    elements.seriesSummaryText.textContent = `Status temporarily unavailable: ${error.message}`;
    scheduleSeriesPoll(5000);
  }
}

function scheduleSeriesPoll(delay) {
  clearTimeout(state.series.pollTimer);
  state.series.pollTimer = setTimeout(refreshActiveSeries, delay);
}

function startSeriesPolling() {
  clearTimeout(state.series.pollTimer);
  refreshActiveSeries();
}

async function seriesAction(action, message) {
  const record = state.series.record;
  if (!record?.id) return;
  setSeriesMessage(message);
  try {
    const result = unwrapSeries(await api(`/api/series/${encodeURIComponent(record.id)}/${action}`, { method: "POST", body: "{}" }));
    if (result?.id) renderSeriesRecord(result);
    else await refreshActiveSeries();
    startSeriesPolling();
    setSeriesMessage(action === "pause" ? "Pause requested; the active shot will finish safely." : "Series resumed.", "success");
  } catch (error) {
    setSeriesMessage(error.message, "error");
  }
}

async function startSavedSeries() {
  const record = state.series.record;
  if (!record?.id || safeStatus(record.status) !== "ready") return;
  elements.startSavedSeries.disabled = true;
  setSeriesMessage("Starting the durable saved series. The separate browser storyboard is unchanged…");
  try {
    const result = unwrapSeries(await api(`/api/series/${encodeURIComponent(record.id)}/start`, { method: "POST", body: "{}" }));
    if (result?.id) renderSeriesRecord(result);
    startSeriesPolling();
    setSeriesMessage("Saved series started; shots will generate one at a time.", "success");
  } catch (error) {
    setSeriesMessage(error.message, "error");
    elements.startSavedSeries.disabled = false;
  }
}

async function cancelActiveSeriesShot() {
  const record = state.series.record;
  if (!record?.id || !Number.isInteger(record.active_shot)) return;
  if (!confirm(`Cancel active Shot ${record.active_shot + 1}? Earlier outputs and every attempt will remain saved.`)) return;
  setSeriesMessage("Requesting a safe cancellation…");
  try {
    const result = unwrapSeries(await api(`/api/series/${encodeURIComponent(record.id)}/cancel-active`, { method: "POST", body: "{}" }));
    if (result?.id) renderSeriesRecord(result);
    else await refreshActiveSeries();
    startSeriesPolling();
    setSeriesMessage("Cancellation requested. Preserved outputs will stay in the timeline.", "success");
  } catch (error) {
    setSeriesMessage(error.message, "error");
  }
}

async function retrySeriesFinalization() {
  const record = state.series.record;
  if (!record?.id) return;
  elements.retrySeriesFinalization.disabled = true;
  setSeriesMessage("Retrying only the final MP4 and manifest. No video shot will regenerate…");
  try {
    const result = unwrapSeries(await api(`/api/series/${encodeURIComponent(record.id)}/retry-finalization`, { method: "POST", body: "{}" }));
    if (result?.id) renderSeriesRecord(result);
    startSeriesPolling();
    setSeriesMessage("Final assembly restarted from the accepted shots; no GPU shot was resubmitted.", "success");
  } catch (error) {
    setSeriesMessage(error.message, "error");
    elements.retrySeriesFinalization.disabled = false;
  }
}

async function retrySeriesShot(seriesId, shotIndex, regenerateFollowing) {
  const action = regenerateFollowing ? "Regenerate this shot and rebuild every following continuity link? All old attempts stay saved." : "Retry this failed shot? Its earlier attempts stay saved.";
  if (!confirm(action)) return;
  setSeriesMessage(regenerateFollowing ? `Preparing to regenerate from Shot ${shotIndex + 1}…` : `Preparing to retry Shot ${shotIndex + 1}…`);
  try {
    const result = unwrapSeries(await api(`/api/series/${encodeURIComponent(seriesId)}/shots/${encodeURIComponent(shotIndex)}/retry`, {
      method: "POST",
      body: JSON.stringify({ regenerate_following: regenerateFollowing }),
    }));
    if (result?.id) renderSeriesRecord(result);
    startSeriesPolling();
    setSeriesMessage("Retry accepted. Preserved attempts remain available below the shot.", "success");
  } catch (error) {
    setSeriesMessage(error.message, "error");
  }
}

async function refreshSeriesLibrary() {
  if (!state.config) return;
  try {
    const result = await api("/api/series");
    state.series.library = Array.isArray(result) ? result : Array.isArray(result.series) ? result.series : Array.isArray(result.items) ? result.items : [];
    renderSeriesLibrary();
  } catch (error) {
    elements.seriesLibrary.replaceChildren();
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = error.status === 404 ? "Series support is not available in this server build." : `Could not load saved series: ${error.message}`;
    elements.seriesLibrary.append(empty);
  }
}

function renderSeriesLibrary() {
  elements.seriesLibrary.replaceChildren();
  if (!state.series.library.length) {
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = "No local series yet.";
    elements.seriesLibrary.append(empty);
    return;
  }
  for (const record of state.series.library) {
    if (!record?.id) continue;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "series-library-card";
    const copy = document.createElement("span");
    const strong = document.createElement("strong");
    strong.textContent = record.title || "Untitled series";
    const small = document.createElement("small");
    const count = Array.isArray(record.shots) ? record.shots.length : Number(record.shot_count) || 0;
    const date = new Date(Number(record.updated_ms));
    const updated = Number.isNaN(date.valueOf()) ? "saved locally" : `updated ${date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}`;
    small.textContent = `${count} shot${count === 1 ? "" : "s"} · ${updated}`;
    copy.append(strong, small);
    const status = document.createElement("span");
    status.textContent = safeStatus(record.status).replaceAll("_", " ");
    button.append(copy, status);
    button.addEventListener("click", () => openSeries(record.id));
    elements.seriesLibrary.append(button);
  }
}

async function openSeries(seriesId) {
  switchWorkflow("series", { focus: false });
  elements.seriesSummaryText.textContent = "Loading the saved director board…";
  try {
    const record = unwrapSeries(await api(`/api/series/${encodeURIComponent(seriesId)}`));
    renderSeriesRecord(record);
    if (seriesBlockingStatus(safeStatus(record.status)) || safeStatus(record.status) === "paused") startSeriesPolling();
    elements.seriesOutputWorkspace.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setSeriesMessage(error.message, "error");
  }
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
  elements.singleWorkflowTab.addEventListener("click", () => switchWorkflow("single"));
  elements.seriesWorkflowTab.addEventListener("click", () => switchWorkflow("series"));
  elements.form.addEventListener("submit", submitRender);
  elements.seriesComposer.addEventListener("submit", submitSeries);
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
  for (const radio of $$('input[name="seriesTemplate"]')) radio.addEventListener("change", () => applySeriesTemplate(radio.value));
  for (const input of [elements.seriesTitle, elements.seriesBrief]) {
    input.addEventListener("input", () => { updateSeriesReview(); saveSeriesDraftSoon(); });
  }
  for (const select of [elements.seriesProfile, elements.seriesResolution, elements.seriesRefImageSize]) {
    select.addEventListener("change", () => { updateSeriesReview(); saveSeriesDraftSoon(); });
  }
  elements.seriesContinuity.addEventListener("change", () => {
    state.series.templateContinuity[state.series.template] = elements.seriesContinuity.value;
    renderSeriesShots();
    saveSeriesDraftSoon();
  });
  $("#addSeriesShot").addEventListener("click", addSeriesShot);
  for (const input of $$('input[data-series-kind]')) input.addEventListener("change", () => handleSeriesExtraUpload(input));
  $("#refreshSeries").addEventListener("click", refreshActiveSeries);
  $("#refreshSeriesLibrary").addEventListener("click", refreshSeriesLibrary);
  elements.pauseSeries.addEventListener("click", () => seriesAction("pause", "The current shot will finish before pausing…"));
  elements.resumeSeries.addEventListener("click", () => seriesAction("resume", "Resuming the next queued shot…"));
  elements.startSavedSeries.addEventListener("click", startSavedSeries);
  elements.retrySeriesFinalization.addEventListener("click", retrySeriesFinalization);
  elements.cancelSeriesActive.addEventListener("click", cancelActiveSeriesShot);
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
