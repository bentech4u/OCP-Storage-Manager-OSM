// Small helpers: theme toggle, job console streaming, confirm dialogs.
(function () {
  const root = document.documentElement;
  const saved = localStorage.getItem("csm-theme");
  if (saved) root.setAttribute("data-theme", saved);

  window.toggleTheme = function () {
    const next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
    root.setAttribute("data-theme", next);
    localStorage.setItem("csm-theme", next);
  };

  // Live job output. Opens an EventSource and appends lines as they arrive.
  window.followJob = function (jobId, targetId, statusId) {
    const box = document.getElementById(targetId);
    const status = statusId ? document.getElementById(statusId) : null;
    if (!box) return;
    const src = new EventSource(`/api/jobs/${jobId}/stream`);
    src.onmessage = function (ev) {
      const data = JSON.parse(ev.data);
      (data.lines || []).forEach(function (line) {
        const el = document.createElement("div");
        if (line.startsWith("$ ")) el.className = "cmd";
        else if (/error|failed|Error|FAIL/.test(line)) el.className = "err";
        el.textContent = line;
        box.appendChild(el);
      });
      box.scrollTop = box.scrollHeight;
      if (data.status && data.status !== "running") {
        if (status) {
          status.className = "pill " + (data.status === "ok" ? "ok" : "bad");
          status.textContent = data.status === "ok" ? "finished" : "failed";
        }
        src.close();
        document.body.dispatchEvent(new CustomEvent("job-finished", { detail: data }));
      }
    };
    src.onerror = function () { src.close(); };
  };

  // Any element with data-confirm asks first.
  document.body.addEventListener("htmx:confirm", function (evt) {
    const q = evt.detail.elt.getAttribute("data-confirm");
    if (!q) return;
    evt.preventDefault();
    if (window.confirm(q)) evt.detail.issueRequest(true);
  });

  // Keep a paired pair of cluster pickers on two different clusters.
  document.addEventListener("change", function (evt) {
    const pair = evt.target.getAttribute && evt.target.getAttribute("data-pair");
    if (!pair) return;
    const other = document.querySelector('[name="' + pair + '"]');
    if (!other || other.value !== evt.target.value) return;
    const choice = Array.from(other.options).find(function (o) { return o.value !== evt.target.value; });
    if (choice) other.value = choice.value;
  });

  // Refresh panels that ask for it when a job ends.
  document.body.addEventListener("job-finished", function () {
    document.querySelectorAll("[data-refresh-on-job]").forEach(function (el) {
      if (window.htmx) window.htmx.trigger(el, "refresh");
    });
  });
})();
