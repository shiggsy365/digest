(function () {
  'use strict';
  var state = {page: 1, pages: [], index: 0, more: false, query: '', libraryExtra: '',
    total: 0, pageSize: 40, pendingLast: false};
  var content = document.getElementById('spa-content');
  var filters = document.getElementById('spa-filters');
  var token = document.querySelector('meta[name="csrf-token"]').getAttribute('content');

  function esc(value) {
    var node = document.createElement('div');
    node.appendChild(document.createTextNode(value == null ? '' : String(value)));
    return node.innerHTML;
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
  function row(book) {
    var cover = book.cover_url ? '<img src="' + esc(book.cover_url) + '" alt="">' : '';
    var author = book.author || (book.authors || [])[0] || '';
    if (!book.id) return '<li class="book-item">' + cover + '<b>' + esc(book.title) +
      '</b><div>' + esc(author) + '</div><button data-want="1" data-source="' +
      esc(book.source || 'openlibrary') + '" data-source-id="' + esc(book.source_id || '') +
      '" data-title="' + esc(book.title) + '" data-author="' + esc(author) +
      '" data-cover="' + esc(book.cover_url || '') + '">Request download</button></li>';
    return '<article class="book-row">' + (cover ? '<div class="book-cover">' + cover + '</div>' : '') +
      '<div class="book-body"><h3><a href="#book/' + esc(book.id) + '">' + esc(book.title) +
      '</a></h3><p>' + esc(author) + (book.series ? ' &middot; ' + esc(book.series) +
      (book.series_number ? ' #' + esc(book.series_number) : '') : '') + '</p></div></article>';
  }
  function renderPage() {
    var list = content.querySelector('[data-paginate]');
    var range = state.pages[state.index] || [0, 0];
    var i;
    if (list) for (i = 0; i < list.children.length; i++)
      list.children[i].style.display = i >= range[0] && i < range[1] ? '' : 'none';
    document.getElementById('page-label').innerHTML = state.pages.length ?
      'Page ' + (state.index + 1) + ' of ' + state.pages.length + (state.more ? '+' : '') : '';
  }
  function paginate() {
    var list = content.querySelector('[data-paginate]');
    var size, i;
    state.pages = [];
    state.index = 0;
    if (list && list.children.length) {
      size = Math.max(1, Math.floor((content.clientHeight - list.offsetTop - 6) /
        (list.children[0].offsetHeight || 55)));
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
    api('/library?page=' + state.page + '&q=' + encodeURIComponent(state.query) + state.libraryExtra,
      function (error, data) {
        if (error) return fail(error);
        state.more = data.has_more;
        state.total = data.total;
        state.pageSize = data.page_size;
        filters.innerHTML = '<div class="section-tabs"><button data-library="latest">Recent</button> ' +
          '<button data-library="reading">Reading</button> <button data-library="favourites">Favourites</button> ' +
          '<button data-library="rated">Rated</button> <button data-library="all">All Books</button> ' +
          '<button data-directory="authors">Authors</button> <button data-directory="series">Series</button></div>';
        match = /[?&]view=([^&]+)/.exec(state.libraryExtra);
        showBooks(state.query ? 'Search results' : (labels[match ? match[1] : 'latest'] || 'Library'), data.items);
      });
  }
  function directory(kind) {
    api('/library/' + kind, function (error, data) {
      if (error) return fail(error);
      state.more = false;
      content.innerHTML = '<main><h1>All ' + esc(kind) +
        '</h1><div class="directory-grid" data-paginate>' + data.items.map(function (item) {
          return '<button class="directory-tile" data-filter="' + kind + '" data-value="' +
            esc(item.name) + '"><strong>' + esc(item.name) + '</strong><span>' +
            item.count + (item.count === 1 ? ' book' : ' books') + '</span></button>';
        }).join('') + '</div></main>';
      paginate();
    });
  }
  function discover(group) {
    var titles = {'for-you': 'For you', trending: 'Trending', 'new-releases': 'New releases',
      'genre?genre=fantasy': 'Fantasy'};
    group = group || 'for-you';
    api('/discover/' + group, function (error, data) {
      if (error) return fail(error);
      filters.innerHTML = '<button data-discover="for-you">For you</button> ' +
        '<button data-discover="trending">Trending</button> ' +
        '<button data-discover="new-releases">New releases</button> ' +
        '<button data-discover="genre?genre=fantasy">Fantasy</button>';
      state.more = false;
      showBooks(titles[group] || 'Discover', data.items);
    });
  }
  function shelves() {
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
  function shelf(id) { api('/shelves/' + id, function (error, data) {
    if (error) return fail(error); showBooks(data.shelf.name, data.items);
  }); }
  function book(id) {
    api('/books/' + id, function (error, data) {
      var reading;
      if (error) return fail(error);
      reading = data.reading || {state: 'unread', rating: 0, favourite: false};
      filters.innerHTML = '';
      content.innerHTML = '<main><h1>' + esc(data.title) + '</h1><p>' + esc(data.author) +
        '</p><p>' + esc(data.description) + '</p><form id="reading-form" data-book="' +
        esc(data.id) + '"><select name="state">' +
        ['unread', 'reading', 'finished', 'abandoned', 'want-to-read'].map(function (value) {
          return '<option' + (reading.state === value ? ' selected' : '') + '>' + value + '</option>';
        }).join('') + '</select> <select name="rating">' + [0, 1, 2, 3, 4, 5].map(function (value) {
          return '<option value="' + value + '"' + ((reading.rating || 0) === value ? ' selected' : '') +
            '>' + (value || 'No rating') + '</option>';
        }).join('') + '</select> <label><input type="checkbox" name="favourite"' +
        (reading.favourite ? ' checked' : '') + '> Favourite</label> <button>Save</button></form>' +
        '<div class="actions">' + data.files.map(function (file) { return '<a href="' +
          file.download_url + '">Download ' + esc(file.format.toUpperCase()) + '</a>'; }).join('') +
        '</div><div class="actions">' + data.shelves.map(function (item) {
          var on = data.shelf_ids.indexOf(item.id) !== -1;
          return '<button data-shelf-book="' + item.id + '" data-book="' + esc(data.id) +
            '" data-method="' + (on ? 'DELETE' : 'POST') + '">' + (on ? 'Remove from ' : 'Add to ') +
            esc(item.name) + '</button>';
        }).join('') + '</div></main>';
      state.pages = []; renderPage();
    });
  }
  function downloads() {
    api('/downloads', function (error, data) {
      if (error) return fail(error);
      filters.innerHTML = '';
      content.innerHTML = '<main><h1>Downloads</h1><ul class="download-list" data-paginate>' +
        data.items.map(function (item) {
          var done = item.status === 'available' || item.status === 'failed';
          return '<li class="download-item"><b>' + esc(item.title) + '</b><div>' + esc(item.author) +
            '</div><span class="muted">' + esc(item.status) + '</span><div class="actions">' +
            (item.status === 'failed' ? '<button data-download="retry" data-id="' + item.id +
            '">Retry</button>' : '') + '<button data-download="' + (done ? 'remove' : 'cancel') +
            '" data-id="' + item.id + '">' + (done ? 'Remove' : 'Cancel') + '</button></div></li>';
        }).join('') + '</ul></main>';
      paginate();
    });
  }
  function profile() {
    api('/settings', function (error, data) {
      if (error) return fail(error);
      filters.innerHTML = '';
      content.innerHTML = '<main><h1>Settings</h1><form id="settings-form"><div class="form-row">' +
        '<label>Kindle email</label><input name="kindle_email" value="' + esc(data.kindle_email) +
        '"></div><div class="form-row"><label>Kobo sync</label><select name="kobo_sync_shelf_id">' +
        '<option value="">Disabled</option><option value="all"' +
        (data.kobo_sync_all_books ? ' selected' : '') + '>All books</option>' +
        data.shelves.map(function (item) { return '<option value="' + item.id + '"' +
          (item.id === data.kobo_sync_shelf_id ? ' selected' : '') + '>' + esc(item.name) +
          '</option>'; }).join('') + '</select></div><button>Save</button> <button type="button" ' +
        'id="kobo-token">' + (data.kobo_configured ? 'Replace' : 'Issue') + ' Kobo token</button>' +
        (data.kobo_configured ? ' <button type="button" id="kobo-revoke">Revoke Kobo token</button>' : '') +
        '</form><p id="settings-message"></p></main>';
      state.pages = []; renderPage();
    });
  }
  function route() {
    var hash = location.hash.slice(1) || 'library';
    var parts = hash.split('/');
    state.page = 1;
    if (hash === 'library') { state.libraryExtra = ''; library(''); } else if (hash === 'discover') discover();
    else if (hash === 'shelves') shelves(); else if (hash === 'downloads') downloads();
    else if (hash === 'settings') profile(); else if (parts[0] === 'book') book(parts[1]);
    else if (parts[0] === 'shelf') shelf(parts[1]); else library();
  }
  document.onclick = function (event) {
    var target = event.target, value;
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
    if (target.id === 'menu-toggle') { value = document.getElementById('spa-menu');
      value.className = value.className ? '' : 'hidden'; }
    if (target.id === 'search-toggle') { value = document.getElementById('spa-search');
      value.className = value.className ? '' : 'hidden'; }
    if (target.id === 'kobo-token') ajax('POST', '/api/ereader/settings/kobo-token', null,
      function (error, data) { document.getElementById('settings-message').innerHTML = error ?
        esc(error) : 'Kobo endpoint: ' + esc(data.endpoint); });
    if (target.id === 'kobo-revoke') ajax('DELETE', '/api/ereader/settings/kobo-token', null,
      function (error) { if (error) fail(error); else profile(); });
    if ((value = target.getAttribute('data-page'))) {
      if (value === 'first') {
        if (state.page > 1) { state.page = 1; library(); return; }
        state.index = 0;
      } else if (value === 'prev' && state.index > 0) state.index--;
      else if (value === 'prev' && state.page > 1) {
        state.page--; state.pendingLast = true; library(); return;
      }
      else if (value === 'next' && state.index < state.pages.length - 1) state.index++;
      else if (value === 'next' && state.more) { state.page++; library(); return; }
      else if (value === 'last') {
        value = Math.max(1, Math.ceil(state.total / state.pageSize));
        if (state.page !== value) { state.page = value; state.pendingLast = true; library(); return; }
        state.index = state.pages.length - 1;
      }
      renderPage();
    }
  };
  document.getElementById('spa-search').onsubmit = function (event) {
    event.preventDefault(); state.query = this.elements.q.value; location.hash = 'library'; library();
  };
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (form.id === 'settings-form') { event.preventDefault(); ajax('PUT', '/api/ereader/settings',
      {kindle_email: form.elements.kindle_email.value,
        kobo_sync_shelf_id: form.elements.kobo_sync_shelf_id.value}, function (error) {
        document.getElementById('settings-message').innerHTML = error ? esc(error) : 'Saved'; }); }
    else if (form.id === 'shelf-form') { event.preventDefault(); ajax('POST', '/api/ereader/shelves',
      {name: form.elements.name.value}, function (error) { if (error) fail(error); else shelves(); }); }
    else if (form.id === 'reading-form') { event.preventDefault(); ajax('PUT', '/api/ereader/books/' +
      form.getAttribute('data-book') + '/reading-state', {state: form.elements.state.value,
        rating: form.elements.rating.value, favourite: form.elements.favourite.checked},
      function (error) { if (error) fail(error); else book(form.getAttribute('data-book')); }); }
  });
  window.onhashchange = route;
  window.onresize = paginate;
  route();
}());
