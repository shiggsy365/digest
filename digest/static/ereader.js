function fitDigestShell() {
    var content = document.getElementById('shell-content');
    var shell = document.getElementById('app-shell');
    if (!content || !shell) return;
    var h = window.innerHeight || document.documentElement.clientHeight;
    if (!h) return;
    var used = 0;
    var rows = shell.getElementsByClassName('shell-row');
    for (var i = 0; i < rows.length; i++) {
        if (rows[i].id !== 'shell-content-row') used += rows[i].offsetHeight;
    }
    var contentH = h - used;
    content.style.height = (contentH > 80 ? contentH : 80) + 'px';
    fitPagedLists();
}

function hasClass(el, cls) {
    return (' ' + el.className + ' ').indexOf(' ' + cls + ' ') !== -1;
}

function addClass(el, cls) {
    if (!hasClass(el, cls)) el.className = (el.className + ' ' + cls).replace(/^\s+/, '');
}

function removeClass(el, cls) {
    var parts = el.className.split(' ');
    var kept = [];
    for (var i = 0; i < parts.length; i++) {
        if (parts[i] !== cls) kept.push(parts[i]);
    }
    el.className = kept.join(' ');
}

function toggleNavSearch() {
    var bar = document.getElementById('main-nav-bar');
    var form = document.getElementById('nav-search-form');
    if (!bar || !form) return;
    var showingSearch = !hasClass(form, 'hidden');
    if (showingSearch) {
        addClass(form, 'hidden');
        removeClass(bar, 'hidden');
    } else {
        addClass(bar, 'hidden');
        removeClass(form, 'hidden');
        var input = form.querySelector('input[name="q"]');
        if (input) input.focus();
    }
    fitDigestShell();
}

function toggleBurgerMenu() {
    var menu = document.getElementById('burger-menu');
    if (!menu) return;
    if (hasClass(menu, 'hidden')) removeClass(menu, 'hidden');
    else addClass(menu, 'hidden');
}

document.addEventListener('click', function (event) {
    var menu = document.getElementById('burger-menu');
    var btn = document.getElementById('nav-burger-btn');
    if (!menu || hasClass(menu, 'hidden')) return;
    if (menu.contains(event.target) || event.target === btn) return;
    addClass(menu, 'hidden');
});

// --- Paginated lists ---
// Each page has at most one list carrying the "js-paginated" class. It fits
// as many of its already-rendered items as the visible content area allows,
// paging locally through them; data-prev-url/data-next-url/data-first-url
// and data-last-url (when present) send the controls to the matching server batch
// once the locally-held items run out.
var pagedListState = null;

function elementTopWithin(element, ancestor) {
    var top = 0;
    while (element && element !== ancestor) {
        top += element.offsetTop || 0;
        element = element.offsetParent;
    }
    return top;
}

function marginBottomOf(el) {
    var style = window.getComputedStyle ? window.getComputedStyle(el) : el.currentStyle;
    return style ? (parseFloat(style.marginBottom) || 0) : 0;
}

function pageCapacity(items, container, content) {
    var available = content.clientHeight - elementTopWithin(container, content) - 8;
    var columns = parseInt(container.getAttribute('data-columns') || '1', 10);
    var width = items[0].offsetWidth || 1;
    var rowTop = null;
    var rowHeight = 0;
    var rows = 0;
    if (columns > 1 && width > 0) {
        columns = Math.max(1, Math.floor(container.clientWidth / width));
    }
    for (var i = 0; i < items.length; i++) {
        var top = items[i].offsetTop;
        if (rowTop === null || top !== rowTop) {
            if (rows && rowHeight && rowHeight > available) break;
            if (rows) available -= rowHeight;
            rowTop = top;
            rowHeight = items[i].offsetHeight + marginBottomOf(items[i]);
            rows++;
        } else {
            rowHeight = Math.max(rowHeight, items[i].offsetHeight + marginBottomOf(items[i]));
        }
        if (available - rowHeight < 0) break;
    }
    return Math.max(columns, Math.max(1, rows) * columns);
}

function fitPagedLists() {
    var content = document.getElementById('shell-content');
    var container = document.getElementsByClassName('js-paginated')[0];
    if (!content || !container || !container.children.length) {
        pagedListState = null;
        renderPagedList();
        return;
    }
    var items = container.children;
    var perPage = container.className.indexOf('book-list') !== -1 ?
        6 : pageCapacity(items, container, content);
    var page = pagedListState && pagedListState.container === container ? pagedListState.page : 0;
    pagedListState = {
        container: container,
        items: items,
        perPage: perPage,
        page: page,
        prevUrl: container.getAttribute('data-prev-url') || '',
        nextUrl: container.getAttribute('data-next-url') || '',
        firstUrl: container.getAttribute('data-first-url') || '',
        lastUrl: container.getAttribute('data-last-url') || ''
    };
    renderPagedList();
}

function renderPagedList() {
    var first = document.getElementById('pg-first');
    var prev = document.getElementById('pg-prev');
    var next = document.getElementById('pg-next');
    var last = document.getElementById('pg-last');
    var label = document.getElementById('pg-label');
    var state = pagedListState;
    if (!state) {
        if (first) first.disabled = true;
        if (prev) prev.disabled = true;
        if (next) next.disabled = true;
        if (last) last.disabled = true;
        if (label) label.textContent = '';
        return;
    }
    var totalLocalPages = Math.max(1, Math.ceil(state.items.length / state.perPage));
    if (state.page >= totalLocalPages) state.page = totalLocalPages - 1;
    for (var i = 0; i < state.items.length; i++) {
        var onPage = Math.floor(i / state.perPage) === state.page;
        state.items[i].style.display = onPage ? '' : 'none';
    }
    var atFirst = state.page === 0 && !state.prevUrl;
    var atLocalLast = state.page === totalLocalPages - 1;
    if (first) first.disabled = atFirst;
    if (prev) prev.disabled = atFirst;
    if (next) next.disabled = atLocalLast && !state.nextUrl;
    if (last) last.disabled = atLocalLast && !state.lastUrl;
    if (label) {
        var text = 'Page ' + (state.page + 1);
        if (totalLocalPages > 1 || state.nextUrl) text += ' of ' + totalLocalPages + (state.nextUrl ? '+' : '');
        label.textContent = text;
    }
}

function pagedGo(direction) {
    var state = pagedListState;
    if (!state) return;
    var totalLocalPages = Math.max(1, Math.ceil(state.items.length / state.perPage));
    if (direction === 'first') {
        if (state.page > 0) state.page = 0;
        else if (state.firstUrl) { window.location.href = state.firstUrl; return; }
    } else if (direction === 'prev') {
        if (state.page > 0) state.page--;
        else if (state.prevUrl) { window.location.href = state.prevUrl; return; }
    } else if (direction === 'next') {
        if (state.page < totalLocalPages - 1) state.page++;
        else if (state.nextUrl) { window.location.href = state.nextUrl; return; }
    } else if (direction === 'last') {
        if (state.lastUrl && !(state.page === totalLocalPages - 1 && !state.nextUrl)) {
            window.location.href = state.lastUrl;
            return;
        }
        state.page = totalLocalPages - 1;
    }
    renderPagedList();
    var content = document.getElementById('shell-content');
    if (content) content.scrollTop = 0;
}

(function () {
    var back = document.getElementById('pg-back');
    var first = document.getElementById('pg-first');
    var prev = document.getElementById('pg-prev');
    var next = document.getElementById('pg-next');
    var last = document.getElementById('pg-last');
    if (back) back.onclick = function () {
        if (window.history.length > 1) window.history.back();
        else window.location.href = '/';
    };
    if (first) first.onclick = function () { pagedGo('first'); };
    if (prev) prev.onclick = function () { pagedGo('prev'); };
    if (next) next.onclick = function () { pagedGo('next'); };
    if (last) last.onclick = function () { pagedGo('last'); };
})();

function copyKoboEndpoint(button) {
    var input = document.getElementById('kobo-endpoint');
    if (!input) return;
    input.focus();
    input.select();
    var done = function (ok) {
        var original = button.textContent;
        button.textContent = ok ? 'Copied' : 'Select and copy manually';
        setTimeout(function () { button.textContent = original; }, 2000);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(input.value).then(function () { done(true); }, function () { done(false); });
    } else {
        var ok = false;
        try { ok = document.execCommand('copy'); } catch (ignore) {}
        done(ok);
    }
}

fitDigestShell();
window.onload = fitDigestShell;
window.onresize = fitDigestShell;
