/* Single-page booking flow. Plain JS, no framework/build step -- kept
   consistent with the rest of this repo. State lives in memory for the
   session; the profile+family portion of it is persisted server-side (see
   db.py) so a returning user on the same browser skips straight past setup.

   There is no login system anywhere in this project, so "who is this
   browser" is a UUID generated once and kept in localStorage. That's enough
   to make a profile persist across visits without building real auth under
   this timeline -- it's per-browser/device identity, not an account. */

// ---------------------------------------------------------------- state --

const CLINIC_TYPES = [
  { value: "physical_therapy", label: "Physical Therapy" },
  { value: "mental_health", label: "Mental Health" },
  { value: "general", label: "General" },
  { value: "ent", label: "ENT" },
  { value: "dentist", label: "Dentist" },
];

const SETUP_STEPS = ["profileMethod", "profileData", "profileVerify", "familyList", "familyVerify"];

const state = {
  userId: null,
  // Family is part of the profile now, like insurance -- collected once and
  // persisted, not gated behind an interpreter question. Whether THIS
  // particular visit needs an interpreter varies day to day, so that choice
  // lives on the booking (state.booking.has_interpreter), asked fresh each
  // time at submission -- never assumed from a past answer.
  profile: { name: "", phone: "", age: "", dob: "", address: "", zipcode: "", insurance_name: "", insurance_id: "" },
  family: [],
  cardFiles: { id: null, insurance: null },
  transcript: "",
  parsed: { availability: [], clinic_type_hint: null },
  booking: { clinic_type: "", availability: [], has_interpreter: null },
};

function getUserId() {
  let id = localStorage.getItem("aslapp_user_id");
  if (!id) {
    id = (crypto.randomUUID ? crypto.randomUUID() : `u-${Date.now()}-${Math.random().toString(16).slice(2)}`);
    localStorage.setItem("aslapp_user_id", id);
  }
  return id;
}

// ------------------------------------------------------------- helpers --

async function apiGet(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}
async function apiPostJson(path, body) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `${path} -> ${res.status}`);
  }
  return res.json();
}
async function apiPostForm(path, formData) {
  const res = await fetch(path, { method: "POST", body: formData });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `${path} -> ${res.status}`);
  }
  return res.json();
}

function showLoading(message) {
  document.getElementById("loadingMsg").textContent = message;
  document.getElementById("loadingOverlay").classList.add("active");
}
function hideLoading() {
  document.getElementById("loadingOverlay").classList.remove("active");
}
function showBanner(id, message) {
  const el = document.getElementById(id);
  el.textContent = message;
  el.classList.add("show");
}
function hideBanner(id) {
  document.getElementById(id).classList.remove("show");
}

function isValidUSPhone(value) {
  const digits = (value || "").replace(/\D/g, "");
  return digits.length === 10 || (digits.length === 11 && digits.startsWith("1"));
}

function computeAge(dobStr) {
  if (!dobStr) return "";
  const dob = new Date(dobStr);
  if (isNaN(dob)) return "";
  const today = new Date();
  let age = today.getFullYear() - dob.getFullYear();
  const m = today.getMonth() - dob.getMonth();
  if (m < 0 || (m === 0 && today.getDate() < dob.getDate())) age--;
  return age >= 0 ? age : "";
}

// ------------------------------------------------------------ screens --

function showScreen(id) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
  document.getElementById(id).classList.add("active");

  const stepBar = document.getElementById("stepBar");
  const idx = SETUP_STEPS.indexOf(id);
  if (idx === -1) {
    stepBar.style.display = "none";
  } else {
    stepBar.style.display = "flex";
    stepBar.innerHTML = "";
    SETUP_STEPS.forEach((_, i) => {
      const dot = document.createElement("div");
      dot.className = "dot" + (i < idx ? " done" : i === idx ? " current" : "");
      stepBar.appendChild(dot);
    });
  }
  window.scrollTo(0, 0);
}

// -------------------------------------------------------- app startup --

async function init() {
  state.userId = getUserId();
  wireUpAll();
  try {
    const res = await apiGet(`/profile/${state.userId}`);
    if (res.exists && res.profile && res.profile.name) {
      state.profile = { ...state.profile, ...res.profile };
      state.family = res.profile.family || [];
      // phone/dob/insurance became required after this profile may have been
      // saved -- if any are missing, send a returning user back to verify
      // instead of silently failing later at booking submission.
      const complete = state.profile.name && state.profile.phone && state.profile.dob
        && state.profile.insurance_name && state.profile.insurance_id && state.profile.zipcode;
      if (complete) {
        showScreen("inputMethod");
        return;
      }
      fillProfileVerifyForm();
      showScreen("profileVerify");
      return;
    }
  } catch (e) {
    // no saved profile yet (or server hiccup) -- fall through to setup, which is the normal first-visit path
  }
  showScreen("profileMethod");
}

// -------------------------------------------------- profile: method --

function wireProfileMethod() {
  document.getElementById("btnUpload").addEventListener("click", () => showScreen("profileUpload"));
  document.getElementById("btnManual").addEventListener("click", () => {
    fillProfileVerifyForm();
    showScreen("profileVerify");
  });
}

// -------------------------------------------------- profile: upload --

function wireProfileUpload() {
  const idInput = document.getElementById("idCardInput");
  const insInput = document.getElementById("insuranceCardInput");

  idInput.addEventListener("change", () => handleCardFile("id", idInput.files[0]));
  insInput.addEventListener("change", () => handleCardFile("insurance", insInput.files[0]));

  document.getElementById("btnUploadBack").addEventListener("click", () => showScreen("profileMethod"));
  document.getElementById("btnUploadContinue").addEventListener("click", onUploadContinue);
  updateUploadContinueState();
}

function handleCardFile(kind, file) {
  if (!file) return;
  state.cardFiles[kind] = file;
  const tile = document.getElementById(kind === "id" ? "idTile" : "insuranceTile");
  tile.classList.add("filled");
  const img = tile.querySelector("img");
  img.src = URL.createObjectURL(file);
  img.style.display = "block";
  updateUploadContinueState();
}

function updateUploadContinueState() {
  document.getElementById("btnUploadContinue").disabled = !(state.cardFiles.id || state.cardFiles.insurance);
}

async function onUploadContinue() {
  hideBanner("uploadError");
  showLoading("Reading your cards…");
  const merged = {};
  try {
    for (const file of [state.cardFiles.id, state.cardFiles.insurance]) {
      if (!file) continue;
      const fd = new FormData();
      fd.append("image", file);
      const fields = await apiPostForm("/extract-card", fd);
      for (const [k, v] of Object.entries(fields)) {
        if (v !== null && v !== "" && (merged[k] === undefined || merged[k] === "" || merged[k] === null)) {
          merged[k] = v;
        }
      }
    }
    state.profile = { ...state.profile, ...merged };
  } catch (e) {
    showBanner("uploadError", "Couldn't read one of the cards automatically -- you can still fill in the details on the next screen.");
  } finally {
    hideLoading();
  }
  fillProfileVerifyForm();
  showScreen("profileVerify");
}

// -------------------------------------------------- profile: verify --

function fillProfileVerifyForm() {
  const p = state.profile;
  document.getElementById("pf_name").value = p.name || "";
  document.getElementById("pf_phone").value = p.phone || "";
  document.getElementById("pf_dob").value = p.dob || "";
  document.getElementById("pf_age").value = p.age || computeAge(p.dob) || "";
  document.getElementById("pf_address").value = p.address || "";
  document.getElementById("pf_zipcode").value = p.zipcode || "";
  document.getElementById("pf_insurance_name").value = p.insurance_name || "";
  document.getElementById("pf_insurance_id").value = p.insurance_id || "";
}

function wireProfileVerify() {
  document.getElementById("pf_dob").addEventListener("change", (e) => {
    const ageField = document.getElementById("pf_age");
    if (!ageField.value) ageField.value = computeAge(e.target.value);
  });
  document.getElementById("btnProfileBack").addEventListener("click", () => showScreen("profileMethod"));
  document.getElementById("btnProfileConfirm").addEventListener("click", () => {
    state.profile = {
      name: document.getElementById("pf_name").value.trim(),
      phone: document.getElementById("pf_phone").value.trim(),
      dob: document.getElementById("pf_dob").value,
      age: document.getElementById("pf_age").value ? Number(document.getElementById("pf_age").value) : "",
      address: document.getElementById("pf_address").value.trim(),
      zipcode: document.getElementById("pf_zipcode").value.trim(),
      insurance_name: document.getElementById("pf_insurance_name").value.trim(),
      insurance_id: document.getElementById("pf_insurance_id").value.trim(),
    };
    if (!state.profile.name || !state.profile.zipcode || !state.profile.dob
        || !state.profile.insurance_name || !state.profile.insurance_id) {
      showBanner("profileError", "Name, date of birth, zip code, and insurance are required.");
      return;
    }
    if (!isValidUSPhone(state.profile.phone)) {
      showBanner("profileError", "Enter a valid 10-digit phone number.");
      return;
    }
    hideBanner("profileError");
    renderFamilyList();
    showScreen("familyList");
  });
}

// -------------------------------------------------------- family list --

function addFamilyRow() {
  state.family.push({ name: "", relation: "", phone: "" });
}

function renderFamilyList() {
  const container = document.getElementById("familyCards");
  const empty = document.getElementById("familyEmptyHint");
  container.innerHTML = "";
  empty.style.display = state.family.length === 0 ? "block" : "none";
  state.family.forEach((member, i) => {
    const card = document.createElement("div");
    card.className = "family-card";
    card.innerHTML = `
      <div class="family-card-head">
        <span>Family member ${i + 1}</span>
        <button class="remove-link" data-i="${i}">Remove</button>
      </div>
      <div class="field"><label>Name</label><input type="text" data-field="name" data-i="${i}" value="${member.name}"></div>
      <div class="field"><label>Relation</label><input type="text" data-field="relation" data-i="${i}" placeholder="e.g. sister" value="${member.relation}"></div>
      <div class="field"><label>Phone number</label><input type="tel" data-field="phone" data-i="${i}" value="${member.phone}"></div>
    `;
    container.appendChild(card);
  });
  container.querySelectorAll("input").forEach((inp) => {
    inp.addEventListener("input", (e) => {
      state.family[Number(e.target.dataset.i)][e.target.dataset.field] = e.target.value;
    });
  });
  container.querySelectorAll(".remove-link").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      state.family.splice(Number(e.target.dataset.i), 1);
      renderFamilyList();
    });
  });
}

function wireFamilyList() {
  document.getElementById("btnAddFamily").addEventListener("click", () => { addFamilyRow(); renderFamilyList(); });
  document.getElementById("btnFamilyBack").addEventListener("click", () => showScreen("profileVerify"));
  document.getElementById("btnFamilyContinue").addEventListener("click", () => {
    const incomplete = state.family.some((m) => !m.name || !m.phone);
    if (incomplete) {
      showBanner("familyError", "Add a name and phone number for each family member, or remove the row.");
      return;
    }
    const badPhone = state.family.some((m) => !isValidUSPhone(m.phone));
    if (badPhone) {
      showBanner("familyError", "Each family member needs a valid 10-digit phone number.");
      return;
    }
    hideBanner("familyError");
    renderFamilyVerify();
    showScreen("familyVerify");
  });
}

function renderFamilyVerify() {
  const container = document.getElementById("familyReview");
  container.innerHTML = state.family.length
    ? state.family.map((m) => `
        <div class="review-row"><span class="k">${m.relation || "Family"}</span><span class="v">${m.name} &middot; ${m.phone}</span></div>
      `).join("")
    : `<p class="hint">No family members added. You can still add some later by editing your profile.</p>`;
}

function wireFamilyVerify() {
  document.getElementById("btnFamilyVerifyBack").addEventListener("click", () => showScreen("familyList"));
  document.getElementById("btnFamilyVerifyConfirm").addEventListener("click", async () => {
    await persistProfile();
    showScreen("inputMethod");
  });
}

async function persistProfile() {
  showLoading("Saving your profile…");
  try {
    await apiPostJson(`/profile/${state.userId}`, { ...state.profile, family: state.family });
  } catch (e) {
    // Non-fatal for the demo flow -- the user can still complete a booking
    // this session even if the save call failed; they'd just have to redo
    // setup on their next visit.
  } finally {
    hideLoading();
  }
}

// ------------------------------------------------------- input method --

function wireInputMethod() {
  const typeBtn = document.getElementById("methodType");
  const signBtn = document.getElementById("methodSign");
  const typedBox = document.getElementById("typedInputWrap");
  const signBox = document.getElementById("signInputWrap");

  typeBtn.addEventListener("click", () => {
    typeBtn.classList.add("active"); signBtn.classList.remove("active");
    typedBox.style.display = "block"; signBox.style.display = "none";
    updateUseButtonState();
  });
  signBtn.addEventListener("click", () => {
    signBtn.classList.add("active"); typeBtn.classList.remove("active");
    typedBox.style.display = "none"; signBox.style.display = "block";
    updateUseButtonState();
  });

  document.getElementById("typedInput").addEventListener("input", updateUseButtonState);
  document.getElementById("btnEditProfile").addEventListener("click", () => showScreen("profileVerify"));
  document.getElementById("btnUseTranscript").addEventListener("click", onUseTranscript);

  wireSignCamera();
}

function currentTranscriptText() {
  const signBox = document.getElementById("signInputWrap");
  if (signBox.style.display !== "none") return words.join(" ");
  return document.getElementById("typedInput").value.trim();
}

function updateUseButtonState() {
  document.getElementById("btnUseTranscript").disabled = currentTranscriptText().length === 0;
}

async function onUseTranscript() {
  const text = currentTranscriptText();
  if (!text) return;
  state.transcript = text;
  if (running) stopSession();
  showLoading("Understanding your request…");
  try {
    state.parsed = await apiPostJson("/parse-booking", { transcript: text });
  } catch (e) {
    state.parsed = { availability: [], clinic_type_hint: null };
    showBanner("bookingError", "Couldn't understand that automatically -- please fill in the details below.");
  } finally {
    hideLoading();
  }
  fillBookingConfirm();
  showScreen("bookingConfirm");
}

// ------------------------------------------------- sign camera (reused) --

const CAPTURE_INTERVAL_MS = 150;
const JPEG_QUALITY = 0.7;
let stream = null, socket = null, captureTimer = null, words = [], running = false;
let preview, videoWrap, statusBadge, mainBtn, undoBtn, clearBtn, transcriptBox, lastWordEl, canvas, ctx;

function wireSignCamera() {
  preview = document.getElementById("preview");
  videoWrap = document.getElementById("videoWrap");
  statusBadge = document.getElementById("statusBadge");
  mainBtn = document.getElementById("mainBtn");
  undoBtn = document.getElementById("undoBtn");
  clearBtn = document.getElementById("clearBtn");
  transcriptBox = document.getElementById("transcriptBox");
  lastWordEl = document.getElementById("lastWord");
  canvas = document.getElementById("captureCanvas");
  ctx = canvas.getContext("2d", { willReadFrequently: true });

  mainBtn.addEventListener("click", () => { if (!running) startSession(); else stopSession(); });
  undoBtn.addEventListener("click", () => { words.pop(); renderTranscript(); updateUseButtonState(); });
  clearBtn.addEventListener("click", () => { words = []; renderTranscript(); lastWordEl.textContent = ""; updateUseButtonState(); });
  renderTranscript();
}

function renderTranscript() {
  transcriptBox.textContent = words.join(" ");
  undoBtn.disabled = words.length === 0;
  clearBtn.disabled = words.length === 0;
}
function setStatus(text, cls) { statusBadge.textContent = text; statusBadge.className = cls || ""; }
function wsUrl() { const proto = location.protocol === "https:" ? "wss:" : "ws:"; return `${proto}//${location.host}/ws/stream`; }

async function startSession() {
  mainBtn.disabled = true;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { width: 480, height: 360 }, audio: false });
    preview.srcObject = stream;
    await new Promise((res) => { preview.onloadedmetadata = res; });
    canvas.width = preview.videoWidth || 480;
    canvas.height = preview.videoHeight || 360;

    socket = new WebSocket(wsUrl());
    socket.binaryType = "arraybuffer";

    socket.onopen = () => {
      running = true;
      setStatus("Watching…");
      mainBtn.textContent = "Stop camera";
      mainBtn.disabled = false;
      captureTimer = setInterval(captureAndSendFrame, CAPTURE_INTERVAL_MS);
    };
    socket.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === "status") {
        videoWrap.classList.toggle("signing", msg.signing);
        setStatus(msg.signing ? "Signing detected…" : "Watching…", msg.signing ? "signing" : "");
      } else if (msg.type === "word") {
        words.push(msg.word);
        renderTranscript();
        updateUseButtonState();
        lastWordEl.textContent = `Last sign: "${msg.word}" (${Math.round(msg.confidence * 100)}% confident)`;
      } else if (msg.type === "error") {
        lastWordEl.textContent = "Server error: " + msg.message;
      }
    };
    socket.onerror = () => { lastWordEl.textContent = "Connection error -- check the server is running."; };
    socket.onclose = () => { if (running) stopSession(); };
  } catch (err) {
    setStatus("Camera blocked");
    lastWordEl.textContent = "Couldn't access the camera: " + err.message;
    mainBtn.disabled = false;
  }
}

function captureAndSendFrame() {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  ctx.drawImage(preview, 0, 0, canvas.width, canvas.height);
  canvas.toBlob(async (blob) => {
    if (!blob || !socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(await blob.arrayBuffer());
  }, "image/jpeg", JPEG_QUALITY);
}

function stopSession() {
  running = false;
  if (captureTimer) clearInterval(captureTimer);
  captureTimer = null;
  if (socket) { socket.onclose = null; socket.close(); socket = null; }
  if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
  preview.srcObject = null;
  videoWrap.classList.remove("signing");
  setStatus("Camera off");
  mainBtn.textContent = "Start camera";
  mainBtn.disabled = false;
}

// -------------------------------------------------------- booking confirm --

function fillBookingConfirm() {
  const p = state.profile;
  document.getElementById("bc_profileSummary").innerHTML = `
    <div class="review-row"><span class="k">Name</span><span class="v">${p.name || "--"}</span></div>
    <div class="review-row"><span class="k">DOB</span><span class="v">${p.dob || "--"}</span></div>
    <div class="review-row"><span class="k">Insurance</span><span class="v">${p.insurance_name || "--"}</span></div>
  `;

  const clinicSelect = document.getElementById("bc_clinicType");
  clinicSelect.innerHTML = CLINIC_TYPES.map((c) => `<option value="${c.value}">${c.label}</option>`).join("");
  if (state.parsed.clinic_type_hint) clinicSelect.value = state.parsed.clinic_type_hint;

  document.getElementById("bc_zipcode").value = p.zipcode || "";

  state.booking.availability = (state.parsed.availability && state.parsed.availability.length)
    ? state.parsed.availability.map((a) => ({ ...a }))
    : [{ day: "", date: "", time: "" }];
  renderAvailabilityRows();

  // Asked fresh every time a request is submitted -- whether they need an
  // interpreter found varies visit to visit, so we never carry over a past answer.
  state.booking.has_interpreter = null;
  document.getElementById("bc_interpreterHave").classList.remove("selected");
  document.getElementById("bc_interpreterNeed").classList.remove("selected");
  document.getElementById("bc_familyReview").style.display = "none";
}

function selectBookingInterpreter(hasOwn) {
  state.booking.has_interpreter = hasOwn;
  document.getElementById("bc_interpreterHave").classList.toggle("selected", hasOwn === true);
  document.getElementById("bc_interpreterNeed").classList.toggle("selected", hasOwn === false);

  const familyReview = document.getElementById("bc_familyReview");
  if (hasOwn === false) {
    familyReview.style.display = "block";
    familyReview.innerHTML = state.family.length
      ? state.family.map((m) => `<div class="review-row"><span class="k">${m.relation || "Family"}</span><span class="v">${m.name} &middot; ${m.phone}</span></div>`).join("")
      : `<p class="hint">No family on file -- we'll search for a nearby interpreter instead.</p>`;
  } else {
    familyReview.style.display = "none";
  }
}

function dayNameFor(dateStr) {
  if (!dateStr) return "Pick a date";
  const d = new Date(dateStr + "T00:00:00");
  return isNaN(d) ? "Pick a date" : d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
}

function renderAvailabilityRows() {
  const container = document.getElementById("availabilityRows");
  container.innerHTML = "";
  state.booking.availability.forEach((row, i) => {
    const wrap = document.createElement("div");
    wrap.style.marginBottom = "10px";
    wrap.innerHTML = `
      <div class="hint" style="margin:0 0 4px;font-weight:700;color:var(--text);" data-day-label="${i}">${row.day || dayNameFor(row.date)}</div>
      <div class="avail-row" style="margin-bottom:0;">
        <div class="field"><input type="date" data-field="date" data-i="${i}" value="${row.date || ""}"></div>
        <div class="field"><input type="time" data-field="time" data-i="${i}" value="${row.time || ""}"></div>
        ${state.booking.availability.length > 1 ? `<button class="remove-link" data-i="${i}">Remove</button>` : ""}
      </div>
    `;
    container.appendChild(wrap);
  });
  container.querySelectorAll("input").forEach((inp) => {
    inp.addEventListener("input", (e) => {
      const i = Number(e.target.dataset.i);
      state.booking.availability[i][e.target.dataset.field] = e.target.value;
      if (e.target.dataset.field === "date") {
        const day = dayNameFor(e.target.value);
        state.booking.availability[i].day = day;
        const label = container.querySelector(`[data-day-label="${i}"]`);
        if (label) label.textContent = day;
      }
    });
  });
  container.querySelectorAll(".remove-link").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      state.booking.availability.splice(Number(e.target.dataset.i), 1);
      renderAvailabilityRows();
    });
  });
}

function wireBookingConfirm() {
  document.getElementById("btnAddAvailability").addEventListener("click", () => {
    state.booking.availability.push({ day: "", date: "", time: "" });
    renderAvailabilityRows();
  });
  document.getElementById("bc_interpreterHave").addEventListener("click", () => selectBookingInterpreter(true));
  document.getElementById("bc_interpreterNeed").addEventListener("click", () => selectBookingInterpreter(false));
  document.getElementById("btnBookingBack").addEventListener("click", () => showScreen("inputMethod"));
  document.getElementById("btnSubmitBooking").addEventListener("click", onSubmitBooking);
}

async function onSubmitBooking() {
  const clinic_type = document.getElementById("bc_clinicType").value;
  const zipcode = document.getElementById("bc_zipcode").value.trim();
  const availability = state.booking.availability.filter((a) => a.date && a.time);

  if (!zipcode || availability.length === 0) {
    showBanner("bookingError", "Zip code and at least one date/time are required.");
    return;
  }
  if (state.booking.has_interpreter === null) {
    showBanner("bookingError", "Please say whether you have an interpreter for this visit.");
    return;
  }
  hideBanner("bookingError");

  const payload = {
    user_id: state.userId,
    name: state.profile.name,
    phone: state.profile.phone || null,
    age: state.profile.age || null,
    dob: state.profile.dob || null,
    insurance_name: state.profile.insurance_name || null,
    insurance_id: state.profile.insurance_id || null,
    clinic_type,
    zipcode,
    availability,
    has_interpreter: state.booking.has_interpreter,
    family: state.booking.has_interpreter ? [] : state.family,
  };

  showLoading("Sending your request…");
  try {
    await apiPostJson("/submit-booking", payload);
    hideLoading();
    showScreen("done");
  } catch (e) {
    hideLoading();
    // Surface the server's actual reason (e.g. a specific bad field) instead
    // of a generic message -- much faster to fix during testing.
    showBanner("bookingError", e.message || "Something went wrong sending this -- please try again.");
  }
}

// --------------------------------------------------------------- done --

function wireDone() {
  document.getElementById("btnBookAnother").addEventListener("click", () => {
    state.transcript = ""; words = [];
    document.getElementById("typedInput").value = "";
    renderTranscript();
    updateUseButtonState();
    showScreen("inputMethod");
  });
}

// --------------------------------------------------------------- wire-up --

function wireUpAll() {
  wireProfileMethod();
  wireProfileUpload();
  wireProfileVerify();
  wireFamilyList();
  wireFamilyVerify();
  wireInputMethod();
  wireBookingConfirm();
  wireDone();
}

init();