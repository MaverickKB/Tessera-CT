/* Native transport presentation. The preload owns privileged operations. */
(() => {
  'use strict';
  if (window.TesseraServer) {
    const controls = document.getElementById("server-controls");
    const start = document.getElementById("server-start");
    const stop = document.getElementById("server-stop");
    const status = document.getElementById("server-status");
    let busy = false;
    let failure = "";
    controls.hidden = false;

    async function refreshServer() {
      if (busy) return;
      try {
        const state = await window.TesseraServer.status();
        start.disabled = state.running;
        stop.disabled = !state.loaded;
        if (state.running) status.textContent = "Running";
        else if (state.loaded) status.textContent = "Server not responding";
        else status.textContent = "Stopped";
        if (failure) status.textContent += ": " + failure;
      } catch (error) {
        start.disabled = true;
        stop.disabled = true;
        status.textContent = String(error);
      }
    }

    async function changeServer(running) {
      busy = true;
      start.disabled = true;
      stop.disabled = true;
      status.textContent = running ? "Starting…" : "Stopping…";
      failure = "";
      try {
        await window.TesseraServer.change(running);
      } catch (error) {
        failure = String(error);
      } finally {
        busy = false;
        await refreshServer();
        if (failure) status.textContent = failure;
      }
    }

    start.addEventListener("click", () => changeServer(true));
    stop.addEventListener("click", () => changeServer(false));
    window.addEventListener("focus", refreshServer);
    refreshServer();
    window.setInterval(refreshServer, 5000);
  } else if (window.TesseraShell && window.TesseraShell.platform) {
    const controls = document.getElementById("server-controls");
    const start = document.getElementById("server-start");
    const stop = document.getElementById("server-stop");
    const status = document.getElementById("server-status");
    if (controls && status) {
      controls.hidden = false;
      if (start) start.hidden = true;
      if (stop) stop.hidden = true;
      const platform = window.TesseraShell.platform;
      if (platform === "macos") {
        status.textContent = "This app starts and stops its own core. LaunchAgent controls appear only when a LaunchAgent plist is configured.";
      } else if (platform === "linux") {
        status.textContent = "This app starts and stops its own core. LaunchAgent start and stop is macOS-only and is not available on Linux.";
      } else if (platform === "windows") {
        status.textContent = "This app starts and stops its own core. LaunchAgent start and stop is macOS-only and is not available on Windows. This window does not install a Windows service.";
      } else {
        status.textContent = "This app starts and stops its own core. System service controls are not available on this operating system.";
      }
    }
  }
  if (!window.TesseraDesktop) return;
  const connection = document.getElementById('settings-connection');
  const legacy = connection?.parentElement?.querySelector('p:nth-of-type(2)');
  if (legacy) legacy.textContent = 'The app starts and stops its own local core.';
  const section = connection?.closest('section');
  const heading = section?.querySelector('h2');
  if (heading) heading.textContent = 'Local core';
  const intro = section?.querySelector('.settings-section-intro p');
  if (intro) intro.textContent = 'Managed automatically by this desktop app.';
  const reading = document.querySelector('#reading-settings-title + p');
  if (reading) reading.textContent = 'These preferences stay in this app.';
  for (const node of document.querySelectorAll('#page-voice .quiet-note')) {
    node.textContent = node.textContent.replace('this tab', 'this window');
  }

  class DesktopVoiceSocket {
    constructor(voiceId) {
      this.readyState = 0;
      this.bufferedAmount = 0;
      this.transport = window.TesseraDesktop.openVoice(voiceId, event => {
        if (event.kind === 'open') {
          this.readyState = 1;
          this.onopen?.();
        } else if (event.kind === 'message') this.onmessage?.({data: event.data});
        else if (event.kind === 'error') this.onerror?.();
        else if (event.kind === 'close') {
          this.readyState = 3;
          this.onclose?.();
        }
      });
    }
    send(value) { this.transport.send(value); }
    close() { this.readyState = 3; this.transport.close(); }
  }
  window.TesseraDesktopVoiceSocket = DesktopVoiceSocket;
})();
