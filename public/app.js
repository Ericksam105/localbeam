(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const senderView = $("#sender-view");
  const receiverView = $("#receiver-view");
  const transferMatch = window.location.pathname.match(/^\/(?:r|receive)\/([A-Za-z0-9_-]+)$/);
  let maxFileSize = 2 * 1024 ** 3;
  let shareOrigin = window.location.origin;
  let selectedFile = null;
  let activeTransfer = null;
  let pollTimer = null;
  let uploadStarted = false;

  function formatBytes(value) {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let amount = Number(value);
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) {
      amount /= 1024;
      index += 1;
    }
    const digits = index === 0 ? 0 : amount >= 10 ? 1 : 2;
    const fixed = amount.toFixed(digits);
    const clean = digits ? fixed.replace(/\.?0+$/, "") : fixed;
    return `${clean} ${units[index]}`;
  }

  function showError(element, message) {
    element.textContent = message;
    element.classList.toggle("hidden", !message);
  }

  async function getJson(url, options) {
    const response = await fetch(url, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status}).`);
    return payload;
  }

  async function loadConfig() {
    try {
      const config = await getJson("/api/config");
      maxFileSize = config.maxFileSizeBytes;
      shareOrigin = config.shareOrigin || window.location.origin;
      $("#limit-chip").textContent = `${formatBytes(maxFileSize)} maximum`;
    } catch (error) {
      $("#limit-chip").textContent = "Limit unavailable";
    }
  }

  function selectFile(file) {
    showError($("#send-error"), "");
    if (!file) return;
    if (file.size > maxFileSize) {
      selectedFile = null;
      $("#file-row").classList.add("hidden");
      $("#create-button").disabled = true;
      showError($("#send-error"), `${file.name} is ${formatBytes(file.size)}. The current limit is ${formatBytes(maxFileSize)}.`);
      return;
    }
    selectedFile = file;
    $("#file-name").textContent = file.name;
    $("#file-size").textContent = formatBytes(file.size);
    $("#file-row").classList.remove("hidden");
    $("#create-button").disabled = false;
  }

  function updateProgress(prefix, value) {
    const percent = Math.max(0, Math.min(100, value));
    $(`#${prefix}-progress`).style.width = `${percent}%`;
    $(`#${prefix}-percent`).textContent = `${Math.round(percent)}%`;
  }

  async function createTransfer() {
    if (!selectedFile) return;
    const button = $("#create-button");
    button.disabled = true;
    button.textContent = "Creating link…";
    showError($("#send-error"), "");
    try {
      activeTransfer = await getJson("/api/transfers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          fileName: selectedFile.name,
          size: selectedFile.size,
          contentType: selectedFile.type || "application/octet-stream",
        }),
      });
      const shareUrl = new URL(activeTransfer.receivePath, `${shareOrigin}/`).href;
      $("#share-link").value = shareUrl;
      const minutes = Math.max(1, Math.round((activeTransfer.expiresAt - Date.now()) / 60000));
      $("#expiry-label").textContent = `Expires in ${minutes} minute${minutes === 1 ? "" : "s"}`;
      $("#drop-zone").classList.add("hidden");
      $("#file-row").classList.add("locked");
      button.classList.add("hidden");
      $("#active-transfer").classList.remove("hidden");
      pollSender();
    } catch (error) {
      button.disabled = false;
      button.textContent = "Create transfer link";
      showError($("#send-error"), error.message);
    }
  }

  async function pollSender() {
    if (!activeTransfer) return;
    try {
      const status = await getJson(`/api/transfers/${activeTransfer.id}`);
      activeTransfer.state = status.state;
      const percent = status.size === 0 ? (status.state === "completed" ? 100 : 0) : (status.bytesSent / status.size) * 100;
      updateProgress("sender", percent);
      const messages = {
        waiting: "Waiting for the recipient to open the link…",
        "receiver-ready": "Recipient connected. Starting transfer…",
        transferring: `Sending ${formatBytes(status.bytesSent)} of ${formatBytes(status.size)}…`,
        completed: "Transfer complete — the link can no longer be used.",
        expired: "This transfer link expired.",
        failed: status.error || "Transfer failed.",
        cancelled: status.error || "Transfer cancelled.",
      };
      $("#sender-status").textContent = messages[status.state] || status.state;
      $("#status-dot").classList.toggle("active", ["receiver-ready", "transferring"].includes(status.state));
      $("#status-dot").classList.toggle("complete", status.state === "completed");
      if (status.state === "receiver-ready" && !uploadStarted) uploadFile();
      if (["completed", "expired", "failed", "cancelled"].includes(status.state)) {
        $("#cancel-button").classList.add("hidden");
        if (status.state === "completed") updateProgress("sender", 100);
        return;
      }
    } catch (error) {
      $("#sender-status").textContent = error.message;
      return;
    }
    pollTimer = window.setTimeout(pollSender, 700);
  }

  function uploadFile() {
    if (!activeTransfer || !selectedFile || uploadStarted) return;
    uploadStarted = true;
    const request = new XMLHttpRequest();
    request.open("PUT", `/api/transfers/${activeTransfer.id}/upload?key=${encodeURIComponent(activeTransfer.uploadKey)}`);
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) updateProgress("sender", (event.loaded / event.total) * 100);
    });
    request.addEventListener("error", () => {
      $("#sender-status").textContent = "The connection was interrupted.";
    });
    request.send(selectedFile);
  }

  async function cancelTransfer() {
    if (!activeTransfer) return;
    try {
      await getJson(`/api/transfers/${activeTransfer.id}/cancel?key=${encodeURIComponent(activeTransfer.uploadKey)}`, { method: "POST" });
      if (pollTimer) window.clearTimeout(pollTimer);
      $("#sender-status").textContent = "Transfer cancelled.";
      $("#cancel-button").classList.add("hidden");
    } catch (error) {
      showError($("#send-error"), error.message);
    }
  }

  function setupSender() {
    receiverView.classList.add("hidden");
    senderView.classList.remove("hidden");
    const dropZone = $("#drop-zone");
    $("#file-input").addEventListener("change", (event) => selectFile(event.target.files[0]));
    ["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => {
      event.preventDefault();
      dropZone.classList.add("dragging");
    }));
    ["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => {
      event.preventDefault();
      dropZone.classList.remove("dragging");
    }));
    dropZone.addEventListener("drop", (event) => selectFile(event.dataTransfer.files[0]));
    $("#remove-file").addEventListener("click", () => {
      selectedFile = null;
      $("#file-input").value = "";
      $("#file-row").classList.add("hidden");
      $("#create-button").disabled = true;
      showError($("#send-error"), "");
    });
    $("#create-button").addEventListener("click", createTransfer);
    $("#copy-button").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText($("#share-link").value);
        $("#copy-button").textContent = "Copied";
        window.setTimeout(() => { $("#copy-button").textContent = "Copy"; }, 1500);
      } catch (_) {
        $("#share-link").select();
      }
    });
    $("#cancel-button").addEventListener("click", cancelTransfer);
    window.addEventListener("beforeunload", (event) => {
      if (activeTransfer && !["completed", "failed", "cancelled", "expired"].includes(activeTransfer.state)) {
        event.preventDefault();
      }
    });
  }

  async function setupReceiver(transferId) {
    senderView.classList.add("hidden");
    receiverView.classList.remove("hidden");
    const button = $("#download-button");
    let transfer;
    try {
      transfer = await getJson(`/api/transfers/${transferId}`);
      $("#receive-name").textContent = transfer.fileName;
      $("#receive-size").textContent = formatBytes(transfer.size);
      if (transfer.state !== "waiting") throw new Error(transfer.error || "This single-use link is no longer available.");
    } catch (error) {
      $("#receive-name").textContent = "Transfer unavailable";
      $("#receive-size").textContent = "";
      button.disabled = true;
      showError($("#receive-error"), error.message);
      return;
    }
    button.addEventListener("click", () => {
      button.disabled = true;
      button.textContent = "Download started";
      $("#receive-progress-wrap").classList.remove("hidden");
      const anchor = document.createElement("a");
      anchor.href = `/api/transfers/${transferId}/download`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      pollReceiver(transferId, transfer.size);
    }, { once: true });
  }

  async function pollReceiver(transferId, size) {
    try {
      const status = await getJson(`/api/transfers/${transferId}`);
      const percent = size === 0 ? (status.state === "completed" ? 100 : 0) : (status.bytesSent / size) * 100;
      updateProgress("receive", percent);
      if (status.state === "completed") {
        updateProgress("receive", 100);
        $("#receive-status").textContent = "Download complete.";
        return;
      }
      if (["failed", "cancelled", "expired"].includes(status.state)) {
        $("#receive-status").textContent = status.error || "Transfer failed.";
        return;
      }
      $("#receive-status").textContent = status.state === "transferring"
        ? `Receiving ${formatBytes(status.bytesSent)} of ${formatBytes(size)}…`
        : "Waiting for the sender…";
    } catch (error) {
      $("#receive-status").textContent = error.message;
      return;
    }
    window.setTimeout(() => pollReceiver(transferId, size), 700);
  }

  loadConfig();
  if (transferMatch) setupReceiver(transferMatch[1]);
  else setupSender();
})();
