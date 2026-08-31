(function () {
  'use strict';
  var state = {page: 1, pages: [], index: 0, more: false, query: '', libraryExtra: '',
    total: 0, pageSize: 6, pendingLast: false, discoveryListing: false, serverPaging: false,
    loader: null, navigation: '', detail: false, previousId: null, nextId: null, lastHash: '',
    originHash: ''};
  var content = document.getElementById('spa-content');
  var filters = document.getElementById('spa-filters');
  var token = document.querySelector('meta[name="csrf-token"]').getAttribute('content');

  function esc(value) {
    var node = document.createElement('div');
    node.appendChild(document.createTextNode(value == null ? '' : String(value)));
    return node.innerHTML;
  }
  function attr(value) {
    return esc(value).replace(/"/g, '&quot;');
  }
  function descriptionPages(text) {
    var value = String(text || 'No description available.');
    var parts = value.split(/\n\s*\n/), pages = [], current = '', i;
    for (i = 0; i < parts.length; i++) {
      if ((current + '\n\n' + parts[i]).length > 900 && current) {
        pages.push(current);
        current = parts[i];
      } else current = current ? current + '\n\n' + parts[i] : parts[i];
    }
    if (current) pages.push(current);
    return pages;
  }
  function ajax(method, url, body, done) {
    var xhr = new XMLHttpRequest();
    xhr.open(method, url, true);
    xhr.setRequestHeader('Accept', 'application/json');
    if (method !== 'GET') {
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.setRequestHeader('X-CSRF-Token', token);
    }
    xhr.onreadystatechange = function () {
      var data = {};
      if (xhr.readyState !== 4) return;
      try { data = JSON.parse(xhr.responseText || '{}'); } catch (ignore) {}
      done(xhr.status >= 200 && xhr.status < 300 ? null : (data.detail || 'Request failed'), data);
    };
    xhr.send(body ? JSON.stringify(body) : null);
  }
  function api(path, done) { ajax('GET', '/api/ereader' + path, null, done); }
  function fail(error) { content.innerHTML = '<main><p class="error">' + esc(error) + '</p></main>'; }
  function setFontSize(size) {
    var html = document.documentElement;
    html.className = html.className.replace(/\bfs-(sm|lg)\b/g, '').replace(/\s+/g, ' ').replace(/^\s+|\s+$/g, '');
    if (size === 'sm' || size === 'lg') html.className = (html.className + ' fs-' + size).replace(/^\s+/, '');
    document.cookie = 'fs=' + size + ';path=/;max-age=31536000;samesite=lax';
    paginate();
  }
  function moreByAuthor(author) {
    if (!author) return '';
    return '<a href="#author?author=' + encodeURIComponent(author) + '">More by this author</a>';
  }
  function row(book) {
    var href;
    var cover = book.cover_url ? '<img src="' + attr(book.cover_url) + '" alt="">' : '';
    var author = book.author || (book.authors || [])[0] || '';
    var badge = state.discoveryListing ? ' <span class="library-status ' +
      (book.in_library ? 'owned' : 'not-owned') + '">' +
      (book.in_library ? 'In library' : 'Not in library') + '</span>' : '';
    if (!book.id) {
      href = '#discover-book?' +
      'source=' + encodeURIComponent(book.source || 'openlibrary') + '&source_id=' +
      encodeURIComponent(book.source_id || '') + '&title=' + encodeURIComponent(book.title) +
      '&author=' + encodeURIComponent(author) + '&isbn=' + encodeURIComponent(book.isbn || '') +
      '&cover_url=' + encodeURIComponent(book.cover_url || '') + '&description=' +
      encodeURIComponent((book.description || '').substring(0, 4000));
      return '<article class="book-row">' +
      (cover ? '<div class="book-cover"><a href="' + attr(href) + '">' + cover + '</a></div>' : '') +
      '<div class="book-body"><h3><a href="' + attr(href) + '">' + esc(book.title) +
      '</a></h3><p>' + esc(author) + badge + '</p></div></article>';
    }
    href = '#book/' + book.id + '?navigation=' + encodeURIComponent(state.navigation);
    return '<article class="book-row">' +
      (cover ? '<div class="book-cover"><a href="' + attr(href) + '">' + cover + '</a></div>' : '') +
      '<div class="book-body"><h3><a href="' + attr(href) + '">' + esc(book.title) +
      '</a></h3><p>' + esc(author) + (book.series ? ' &middot; ' + esc(book.series) +
      (book.series_number ? ' #' + esc(book.series_number) : '') : '') + badge + '</p></div></article>';
  }
  function pageSizeFor(list, fallback) {
    var contentHeight = content.clientHeight || 0;
    var available = contentHeight - (list ? list.offsetTop : 0) - 8;
    var item = list && list.children && list.children.length ? list.children[0] : null;
    var height = item ? item.offsetHeight : 0;
    if (!available || !height) return fallback;
    return Math.max(1, Math.floor(available / Math.max(1, height)));
  }
  function renderPage() {
    var list = content.querySelector('[data-paginate]');
    var range = state.pages[state.index] || [0, 0];
    var first = document.querySelector('[data-page="first"]');
    var prev = document.querySelector('[data-page="prev"]');
    var next = document.querySelector('[data-page="next"]');
    var last = document.querySelector('[data-page="last"]');
    var localFirst = state.index === 0;
    var localLast = state.index >= state.pages.length - 1;
    var serverLast = state.serverPaging && state.total ?
      Math.max(1, Math.ceil(state.total / state.pageSize)) : state.page;
    var i;
    if (list) for (i = 0; i < list.children.length; i++)
      list.children[i].style.display = i >= range[0] && i < range[1] ? '' : 'none';
    document.getElementById('page-label').innerHTML = state.pages.length ?
      'Page ' + (state.serverPaging ? state.page + '.' : '') + (state.index + 1) +
      ' of ' + (state.serverPaging ? serverLast + (state.more ? '+' : '') : state.pages.length) : '';
    if (first) first.disabled = state.serverPaging ? state.page <= 1 && localFirst : localFirst;
    if (prev) prev.disabled = state.serverPaging ? state.page <= 1 && localFirst : localFirst;
    if (next) next.disabled = state.serverPaging ? !state.more && localLast : localLast;
    if (last) last.disabled = state.serverPaging ? serverLast <= state.page && localLast : localLast;
  }
  function paginate() {
    var list = content.querySelector('[data-paginate]');
    var size = parseInt(list && list.getAttribute('data-page-size'), 10) ||
      (list && list.className.indexOf('book-list') !== -1 ? 6 :
      pageSizeFor(list, state.serverPaging ? state.pageSize : 6)), i;
    state.pages = [];
    state.index = 0;
    if (list && list.children.length) {
      for (i = 0; i < list.children.length; i += size)
        state.pages.push([i, Math.min(i + size, list.children.length)]);
    }
    if (state.pendingLast && state.pages.length) {
      state.index = state.pages.length - 1;
      state.pendingLast = false;
    }
    renderPage();
  }
  function showBooks(title, items) {
    state.detail = false;
    content.innerHTML = '<main><h1>' + esc(title) +
      '</h1><div class="book-list" data-paginate>' +
      (items.length ? items.map(row).join('') : '<p class="empty">No books to show here yet.</p>') +
      '</div></main>';
    paginate();
  }
  function library(extra) {
    var labels = {latest: 'Latest books', reading: 'Currently reading', favourites: 'Favourites',
      rated: 'Rated books', all: 'All books'};
    var match;
    if (typeof extra === 'string') state.libraryExtra = extra;
    state.discoveryListing = false;
    state.serverPaging = true;
    state.loader = function () { library(); };
    state.navigation = 'library?q=' + encodeURIComponent(state.query) + state.libraryExtra;
    api('/library?page_size=6&page=' + state.page + '&q=' + encodeURIComponent(state.query) +
      state.libraryExtra,
      function (error, data) {
        if (error) return fail(error);
        state.more = data.has_more;
        state.total = data.total;
        state.pageSize = data.page_size;
        filters.innerHTML = '<div class="section-tabs"><button data-library="latest">Recent</button> ' +
          '<button data-library="all">All Books</button> ' +
          '<button data-directory="authors">Authors</button> <button data-directory="series">Series</button></div>';
        match = /[?&]view=([^&]+)/.exec(state.libraryExtra);
        showBooks(state.query ? 'Search results' : (labels[match ? match[1] : 'latest'] || 'Library'), data.items);
      });
  }
  function directory(kind) {
    state.discoveryListing = false;
    state.detail = false;
    state.serverPaging = false;
    state.loader = null;
    state.page = 1;
    api('/library/' + kind, function (error, data) {
      if (error) return fail(error);
      state.more = false;
      content.innerHTML = '<main><h1>All ' + esc(kind) +
        '</h1><div class="directory-grid" data-paginate data-page-size="20">' +
        data.items.map(function (item) {
          return '<button class="directory-tile" data-filter="' + attr(kind) + '" data-value="' +
            attr(item.name) + '"><strong>' + esc(item.name) + '</strong><span>' +
            item.count + (item.count === 1 ? ' book' : ' books') + '</span></button>';
        }).join('') + '</div></main>';
      paginate();
    });
  }
  function discover(group) {
    var titles = {trending: 'Trending', 'new-releases': 'New releases', genre: 'Genres'};
    var base = group ? group.split('?')[0] : 'trending';
    var subfilters = '';
    group = group || 'trending';
    state.discoveryListing = true;
    state.serverPaging = false;
    state.loader = null;
    state.page = 1;
    api('/discover/' + group, function (error, data) {
      if (error) return fail(error);
      if (base === 'trending') subfilters = '<div class="subfilters">' +
        '<button data-discover="trending?period=now">Now</button> ' +
        '<button data-discover="trending?period=3m">Past 3 months</button> ' +
        '<button data-discover="trending?period=12m">Past 12 months</button> ' +
        '<button data-discover="trending?period=all">All time</button></div>';
      if (base === 'genre') subfilters = '<div class="subfilters">' +
        '<button data-discover="genre?genre=fantasy">Fantasy</button> ' +
        '<button data-discover="genre?genre=science_fiction">Science Fiction</button> ' +
        '<button data-discover="genre?genre=mystery_and_detective_stories">Mystery</button> ' +
        '<button data-discover="genre?genre=romance">Romance</button> ' +
        '<button data-discover="genre?genre=thriller">Thriller</button> ' +
        '<button data-discover="genre?genre=historical_fiction">Historical Fiction</button></div>';
      filters.innerHTML = '<div class="section-tabs"><button data-discover="trending">Trending</button> ' +
        '<button data-discover="new-releases">New releases</button> ' +
        '<button data-discover="genre?genre=fantasy">Genres</button></div>' + subfilters;
      state.more = false;
      showBooks(titles[base] || 'Discover', data.items);
    });
  }
  function authorBooks(author) {
    state.discoveryListing = true;
    state.serverPaging = false;
    state.loader = null;
    state.page = 1;
    api('/discover/author?author=' + encodeURIComponent(author), function (error, data) {
      if (error) return fail(error);
      filters.innerHTML = '';
      state.more = false;
      showBooks('More by ' + data.author, data.items);
    });
  }
  function shelves() {
    state.serverPaging = false;
    state.detail = false;
    state.loader = null;
    api('/shelves', function (error, data) {
      if (error) return fail(error);
      filters.innerHTML = '<form id="shelf-form"><input name="name" required placeholder="New shelf">' +
        '<button>Add</button></form>';
      content.innerHTML = '<main><h1>Shelves</h1><ul class="directory-list" data-paginate>' +
        data.items.map(function (item) { return '<li class="directory-item"><a href="#shelf/' +
          item.id + '">' + esc(item.name) + '</a> <span class="muted">(' + item.count +
          ')</span> <button data-delete-shelf="' + item.id + '">Delete</button></li>'; }).join('') +
        '</ul></main>';
      paginate();
    });
  }
  function shelf(id) { state.serverPaging = true; state.loader = function () { shelf(id); };
    state.navigation = 'shelf:' + id;
    api('/shelves/' + id + '?page_size=6&page=' + state.page, function (error, data) {
    if (error) return fail(error); state.more = data.has_more; state.total = data.total;
    state.pageSize = data.page_size; showBooks(data.shelf.name, data.items);
  }); }
  function book(id, navigation) {
    var pages, actions;
    state.discoveryListing = false;
    state.serverPaging = false;
    state.loader = null;
    state.navigation = navigation || '';
    api('/books/' + id + '?navigation=' + encodeURIComponent(state.navigation), function (error, data) {
      if (error) return fail(error);
      state.detail = true;
      state.previousId = data.previous_id;
      state.nextId = data.next_id;
      pages = descriptionPages(data.description);
      actions = (data.previous_id ? '<a href="#book/' + attr(data.previous_id) + '?navigation=' +
        encodeURIComponent(state.navigation) + '">Previous book</a>' : '') +
        (data.next_id ? '<a href="#book/' + attr(data.next_id) + '?navigation=' +
        encodeURIComponent(state.navigation) + '">Next book</a>' : '') +
        moreByAuthor(data.author) + '<button data-kindle="' + attr(data.id) + '">Send to Kindle</button>';
      filters.innerHTML = '';
      content.innerHTML = '<main><div class="detail-heading">' +
        (data.cover_url ? '<img src="' + attr(data.cover_url) + '" alt="">' : '') +
        '<div><h1>' + esc(data.title) + '</h1><p>' + esc(data.author) + '</p></div></div>' +
        '<div class="description" data-paginate data-page-size="1">' +
        pages.map(function (part) { return '<div class="description-page">' + esc(part) + '</div>'; }).join('') +
        '</div><div class="actions">' + actions +
        '<span id="book-message"></span></div></main>';
      paginate();
    });
  }
  function discoveryBook(query) {
    var pages;
    state.serverPaging = false;
    state.loader = null;
    api('/discover/book?' + query, function (error, data) {
      var target;
      if (error) return fail(error);
      state.discoveryListing = false;
      state.detail = true;
      filters.innerHTML = '';
      pages = descriptionPages(data.description);
      target = data.in_library ? '<button data-kindle="' + attr(data.library_book_id) +
        '">Send to Kindle</button>' : '<button data-want="1" data-source="' + attr(data.source) +
        '" data-source-id="' + attr(data.source_id || '') + '" data-title="' + attr(data.title) +
        '" data-author="' + attr(data.author || '') + '" data-cover="' + attr(data.cover_url || '') +
        '">Request download</button>';
      content.innerHTML = '<main><div class="detail-heading">' +
        (data.cover_url ? '<img src="' + attr(data.cover_url) + '" alt="">' : '') +
        '<div><h1>' + esc(data.title) + '</h1><p>' + esc(data.author || '') + '</p></div></div>' +
        '<div class="description" data-paginate data-page-size="1">' +
        pages.map(function (part) { return '<div class="description-page">' + esc(part) + '</div>'; }).join('') +
        '</div>' +
        '<div class="actions">' + moreByAuthor(data.author) + target +
        '<span id="book-message"></span></div></main>';
      paginate();
    });
  }
  function downloads() {
    state.serverPaging = false;
    state.detail = false;
    state.loader = null;
    api('/downloads', function (error, data) {
      if (error) return fail(error);
      filters.innerHTML = '';
      content.innerHTML = '<main><h1>Downloads</h1><ul class="download-list" data-paginate>' +
        data.items.map(function (item) {
          var done = item.status === 'available' || item.status === 'failed';
          return '<li class="download-item"><b>' + esc(item.title) + '</b><div>' + esc(item.author) +
            '</div><span class="muted">' + esc(item.status) + '</span><div class="actions">' +
            (item.status === 'failed' ? '<button data-download="retry" data-id="' + attr(item.id) +
            '">Retry</button>' : '') + '<button data-download="' + (done ? 'remove' : 'cancel') +
            '" data-id="' + attr(item.id) + '">' + (done ? 'Remove' : 'Cancel') + '</button></div></li>';
        }).join('') + '</ul></main>';
      paginate();
    });
  }
  function profile() {
    var devices;
    state.detail = false;
    api('/settings', function (error, data) {
      if (error) return fail(error);
      devices = data.trusted_devices || [];
      filters.innerHTML = '';
      content.innerHTML = '<main><h1>Settings</h1><form id="settings-form"><div class="form-row">' +
        '<label>Kindle email</label><input name="kindle_email" value="' + attr(data.kindle_email) +
        '"></div><div class="form-row"><label>Kobo sync</label><select name="kobo_sync_shelf_id">' +
        '<option value="">Disabled</option><option value="all"' +
        (data.kobo_sync_all_books ? ' selected' : '') + '>All books</option>' +
        data.shelves.map(function (item) { return '<option value="' + attr(item.id) + '"' +
          (item.id === data.kobo_sync_shelf_id ? ' selected' : '') + '>' + esc(item.name) +
          '</option>'; }).join('') + '</select></div><button>Save</button> <button type="button" ' +
        'id="kobo-token">' + (data.kobo_configured ? 'Replace' : 'Issue') + ' Kobo token</button>' +
        (data.kobo_configured ? ' <button type="button" id="kobo-revoke">Revoke Kobo token</button>' : '') +
        '</form><h2>Remembered devices</h2><p class="muted">Remembered for ' +
        esc(data.trusted_device_days || '') + ' days.</p>' +
        (devices.length ? devices.map(function (device) {
          return '<div class="download-item"><b>' + (device.current ? 'This device' : 'Trusted device') +
            '</b><div>' + esc(device.user_agent) + '</div><span class="muted">Last used ' +
            esc(device.last_used_at) + '</span><div class="actions"><button data-trusted-device="' +
            attr(device.id) + '">Forget device</button></div></div>';
        }).join('') : '<p>No remembered devices.</p>') +
        '<p id="settings-message"></p></main>';
      state.pages = []; renderPage();
    });
  }
  function route() {
    var hash = location.hash.slice(1) || 'library';
    var parts = hash.split('/');
    var bookParts;
    if (hash !== state.lastHash) state.lastHash = hash;
    state.page = 1;
    if (hash === 'library') { state.libraryExtra = ''; library(''); } else if (hash === 'discover') discover('trending');
    else if (hash === 'shelves') shelves(); else if (hash === 'downloads') downloads();
    else if (hash === 'settings') profile(); else if (parts[0] === 'book') {
      bookParts = parts[1].split('?navigation=');
      book(bookParts[0], decodeURIComponent(bookParts[1] || ''));
    }
    else if (hash.indexOf('discover-book?') === 0) discoveryBook(hash.substring(14));
    else if (hash.indexOf('author?author=') === 0)
      authorBooks(decodeURIComponent(hash.substring(14).replace(/\+/g, ' ')));
    else if (parts[0] === 'shelf') shelf(parts[1]); else library();
  }
  document.onclick = function (event) {
    var target = event.target, value;
    if (target.tagName === 'A' && target.getAttribute('href') &&
        target.getAttribute('href').indexOf('#book') === 0) state.originHash = location.hash;
    if (target.tagName === 'A' && target.getAttribute('href') &&
        target.getAttribute('href').indexOf('#discover-book') === 0) state.originHash = location.hash;
    if ((value = target.getAttribute('data-directory'))) directory(value);
    if ((value = target.getAttribute('data-library'))) { state.page = 1; library('&view=' + value); }
    if ((value = target.getAttribute('data-discover'))) discover(value);
    if (target.getAttribute('data-filter')) library('&' +
      (target.getAttribute('data-filter') === 'authors' ? 'author' : 'series') + '=' +
      encodeURIComponent(target.getAttribute('data-value')));
    if ((value = target.getAttribute('data-download'))) ajax('POST', '/api/ereader/downloads/' +
      target.getAttribute('data-id') + '/' + value, null,
      function (error) { if (error) fail(error); else downloads(); });
    if (target.getAttribute('data-want')) ajax('POST', '/api/ereader/downloads',
      {source: target.getAttribute('data-source'), source_id: target.getAttribute('data-source-id'),
        title: target.getAttribute('data-title'), author: target.getAttribute('data-author'),
        cover_url: target.getAttribute('data-cover')},
      function (error) { if (error) fail(error); else location.hash = 'downloads'; });
    if ((value = target.getAttribute('data-delete-shelf'))) ajax('DELETE',
      '/api/ereader/shelves/' + value, null, function (error) { if (error) fail(error); else shelves(); });
    if ((value = target.getAttribute('data-shelf-book'))) ajax(target.getAttribute('data-method'),
      '/api/ereader/shelves/' + value + '/books/' + target.getAttribute('data-book'), null,
      function (error) { if (error) fail(error); else book(target.getAttribute('data-book')); });
    if ((value = target.getAttribute('data-fontsize'))) setFontSize(value);
    if (target.id === 'menu-toggle') { value = document.getElementById('spa-menu');
      value.className = value.className ? '' : 'hidden'; }
    if (target.id === 'search-toggle') { value = document.getElementById('spa-search');
      value.className = value.className ? '' : 'hidden'; }
    if (target.id === 'kobo-token') ajax('POST', '/api/ereader/settings/kobo-token', null,
      function (error, data) { document.getElementById('settings-message').innerHTML = error ?
        esc(error) : 'Kobo endpoint: ' + esc(data.endpoint); });
    if (target.id === 'kobo-revoke') ajax('DELETE', '/api/ereader/settings/kobo-token', null,
      function (error) { if (error) fail(error); else profile(); });
    if ((value = target.getAttribute('data-trusted-device'))) ajax('DELETE',
      '/api/ereader/settings/trusted-devices/' + value, null,
      function (error) { if (error) fail(error); else profile(); });
    if (target.id === 'spa-back') {
      if (state.detail && state.originHash) location.hash = state.originHash;
      else if (history.length > 1) history.back();
      else location.hash = 'library';
    }
    if ((value = target.getAttribute('data-kindle'))) ajax('POST', '/api/ereader/books/' + value +
      '/kindle', null, function (error) {
        document.getElementById('book-message').innerHTML = error ? esc(error) : ' Sent to Kindle.';
      });
    if ((value = target.getAttribute('data-page'))) {
      if (value === 'first') {
        if (state.serverPaging && state.page > 1) { state.page = 1; state.loader(); return; }
        state.index = 0;
      } else if (value === 'prev' && state.index > 0) state.index--;
      else if (value === 'prev' && state.serverPaging && state.page > 1) {
        state.page--; state.pendingLast = true; state.loader(); return;
      }
      else if (value === 'next' && state.index < state.pages.length - 1) state.index++;
      else if (value === 'next' && state.serverPaging && state.more) {
        state.page++; state.loader(); return;
      }
      else if (value === 'last') {
        value = state.serverPaging ? Math.max(1, Math.ceil(state.total / state.pageSize)) : state.page;
        if (state.serverPaging && state.page !== value) {
          state.page = value; state.pendingLast = true; state.loader(); return;
        }
        state.index = state.pages.length - 1;
      }
      renderPage();
    }
  };
  document.getElementById('spa-search').onsubmit = function (event) {
    event.preventDefault();
    state.query = this.elements.q.value;
    if (location.hash.indexOf('#discover') === 0) {
      state.discoveryListing = true;
      api('/discover/search?q=' + encodeURIComponent(state.query), function (error, data) {
        if (error) return fail(error);
        filters.innerHTML = '';
        showBooks('Discovery search results', data.items);
      });
    } else {
      location.hash = 'library';
      library('');
    }
  };
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (form.id === 'settings-form') { event.preventDefault(); ajax('PUT', '/api/ereader/settings',
      {kindle_email: form.elements.kindle_email.value,
        kobo_sync_shelf_id: form.elements.kobo_sync_shelf_id.value}, function (error) {
        document.getElementById('settings-message').innerHTML = error ? esc(error) : 'Saved'; }); }
    else if (form.id === 'shelf-form') { event.preventDefault(); ajax('POST', '/api/ereader/shelves',
      {name: form.elements.name.value}, function (error) { if (error) fail(error); else shelves(); }); }
  });
  window.onhashchange = route;
  window.onresize = paginate;
  route();
}());
