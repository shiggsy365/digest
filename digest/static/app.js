(() => {
  const root = document.documentElement;
  const shell = document.querySelector(".app-shell");
  if (localStorage.getItem("digest-sidebar") === "collapsed") shell?.classList.add("sidebar-collapsed");
  document.querySelector("[data-sidebar-toggle]")?.addEventListener("click", () => {
    const collapsed = shell?.classList.toggle("sidebar-collapsed");
    localStorage.setItem("digest-sidebar", collapsed ? "collapsed" : "expanded");
  });
  const storedSize = localStorage.getItem("digest-cover-size");
  if (storedSize) root.dataset.coverSize = storedSize;

  document.querySelectorAll("[data-cover-size]").forEach((button) => {
    button.addEventListener("click", () => {
      const size = button.dataset.coverSize;
      root.dataset.coverSize = size;
      localStorage.setItem("digest-cover-size", size);
    });
  });

  const panel = document.querySelector("[data-filter-panel]");
  const toggle = document.querySelector("[data-filter-toggle]");
  if (panel && toggle) {
    toggle.addEventListener("click", () => {
      const open = panel.classList.toggle("open");
      toggle.setAttribute("aria-expanded", String(open));
    });
  }

  document.querySelector("[data-select-all]")?.addEventListener("change", (event) => {
    document.querySelectorAll('input[name="book_ids"][form="bulk-library"]').forEach((input) => {
      input.checked = event.target.checked;
    });
  });

  const current = new URL(window.location.href);
  const preserved = ["state", "shelf", "file_format"];
  document.querySelectorAll("a.sort-link").forEach((link) => {
    const target = new URL(link.href);
    preserved.forEach((name) => {
      if (current.searchParams.has(name)) target.searchParams.set(name, current.searchParams.get(name));
    });
    link.href = target.toString();
  });

  document.querySelectorAll(".book-card > a").forEach((link) => {
    link.addEventListener("click", () => sessionStorage.setItem(`digest-scroll:${current.pathname}${current.search}`, String(window.scrollY)));
  });
  document.querySelector("[data-load-more]")?.addEventListener("click", async (event) => {
    event.preventDefault();
    const button = event.currentTarget;
    button.textContent = "Loading…";
    button.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(button.href, {headers: {Accept: "text/html"}});
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      page.querySelectorAll(".grid .book-card").forEach((card) => document.querySelector(".grid")?.appendChild(card));
      const next = page.querySelector("[data-load-more]");
      if (next) {
        button.href = next.href;
        button.textContent = "Load more";
        button.removeAttribute("aria-busy");
      } else {
        button.closest(".catalog-pagination")?.remove();
      }
    } catch (_) {
      button.textContent = "Try again";
      button.removeAttribute("aria-busy");
    }
  });
  const savedScroll = sessionStorage.getItem(`digest-scroll:${current.pathname}${current.search}`);
  if (savedScroll) {
    requestAnimationFrame(() => window.scrollTo(0, Number(savedScroll)));
    sessionStorage.removeItem(`digest-scroll:${current.pathname}${current.search}`);
  }

  fetch("/api/navigation", {headers: {Accept: "application/json"}})
    .then((response) => response.ok ? response.json() : null)
    .then((data) => {
      if (!data) return;
      Object.entries(data.counts).forEach(([name, count]) => {
        const target = document.querySelector(`[data-nav-count="${name}"]`);
        if (target) {
          target.textContent = count;
          if (["review", "downloads"].includes(name)) target.closest("a")?.classList.toggle("nav-attention", Number(count) > 0);
        }
      });
      const shelfList = document.querySelector("[data-sidebar-shelves]");
      if (shelfList) data.shelves.forEach((shelf) => {
        const link = document.createElement("a");
        link.href = shelf.id === "all" ? "/?view=all" : `/shelves/${shelf.id}`;
        link.textContent = shelf.name;
        shelfList.appendChild(link);
      });
    })
    .catch(() => {});

  const message = current.searchParams.has("sent") ? "Book sent successfully." : current.searchParams.has("updated") ? "Changes saved." : "";
  if (message) {
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = message;
    document.querySelector(".toast-region")?.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
  }
})();
