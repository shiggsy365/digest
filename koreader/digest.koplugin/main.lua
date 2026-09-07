local BD = require("ui/bidi")
local DataStorage = require("datastorage")
local DocumentRegistry = require("document/documentregistry")
local InputDialog = require("ui/widget/inputdialog")
local InfoMessage = require("ui/widget/infomessage")
local JSON = require("json")
local LuaSettings = require("luasettings")
local Menu = require("ui/widget/menu")
local NetworkMgr = require("ui/network/manager")
local TextViewer = require("ui/widget/textviewer")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local ffiUtil = require("ffi/util")
local http = require("socket.http")
local ltn12 = require("ltn12")
local socket = require("socket")
local socketurl = require("socket.url")
local socketutil = require("socketutil")
local util = require("util")
local _ = require("gettext")
local T = ffiUtil.template

local https_ok, https = pcall(require, "ssl.https")
local settings = LuaSettings:open(DataStorage:getSettingsDir() .. "/digest.lua")
local opds_settings = LuaSettings:open(DataStorage:getSettingsDir() .. "/opds.lua")
local patched_readerlink = false

local default_opds_servers = {
    {
        title = "Project Gutenberg",
        url = "https://m.gutenberg.org/ebooks.opds/?format=opds",
    },
    {
        title = "Standard Ebooks",
        url = "https://standardebooks.org/feeds/opds",
    },
    {
        title = "ManyBooks",
        url = "http://manybooks.net/opds/index.php",
    },
    {
        title = "Internet Archive",
        url = "https://bookserver.archive.org/",
    },
    {
        title = "textos.info (Spanish)",
        url = "https://www.textos.info/catalogo.atom",
    },
    {
        title = "Gallica (French)",
        url = "https://gallica.bnf.fr/opds",
    },
}

local Digest = WidgetContainer:extend{
    name = "digest",
    is_doc_only = false,
}

local function trim(value)
    return (value or ""):gsub("^%s+", ""):gsub("%s+$", "")
end

local function base_url()
    return trim(settings:readSetting("server_url")):gsub("/+$", "")
end

local function api_token()
    local value = trim(settings:readSetting("api_token"))
    value = value:gsub("^[Aa]uthorization:%s*", "")
    value = value:gsub("^[Bb]earer%s+", "")
    return trim(value)
end

local function digest_username()
    return trim(settings:readSetting("username"))
end

local function encode(value)
    return socketurl.escape(tostring(value or ""))
end

local function text_value(value, fallback)
    if type(value) == "string" then
        return value
    end
    if type(value) == "number" or type(value) == "boolean" then
        return tostring(value)
    end
    return fallback or ""
end

local function number_value(value)
    return type(value) == "number" and value or tonumber(text_value(value))
end

local function table_value(value)
    return type(value) == "table" and value or {}
end

local discover_genres = {
    { label = _("Fantasy"), slug = "fantasy", provider = "Fantasy" },
    { label = _("Science Fiction"), slug = "science_fiction", provider = "Science Fiction" },
    { label = _("Mystery"), slug = "mystery_and_detective_stories", provider = "Mystery" },
    { label = _("Thriller"), slug = "thriller", provider = "Thriller" },
    { label = _("Romance"), slug = "romance", provider = "Romance" },
    { label = _("Historical Fiction"), slug = "historical_fiction", provider = "Historical Fiction" },
    { label = _("Horror"), slug = "horror", provider = "Horror" },
    { label = _("Biography & Memoirs"), slug = "biography", provider = "Biography & Memoirs" },
    { label = _("History"), slug = "history", provider = "History" },
    { label = _("Self Help"), slug = "self_help", provider = "Self Help" },
    { label = _("True Crime"), slug = "true_crime", provider = "True Crime" },
}

local trending_periods = {
    { label = _("Now"), key = "now" },
    { label = _("Past 3 Months"), key = "3m" },
    { label = _("Past 12 Months"), key = "12m" },
    { label = _("All Time"), key = "all" },
}

local new_release_periods = {
    { label = _("Past 30 Days"), key = "30d" },
    { label = _("Past 90 Days"), key = "90d" },
    { label = _("Past 6 Months"), key = "180d" },
    { label = _("Past Year"), key = "365d" },
}

local function join_url(path)
    local base = base_url()
    if base == "" then
        return nil
    end
    if path:sub(1, 1) ~= "/" then
        path = "/" .. path
    end
    return base .. path
end

local function message(text)
    UIManager:show(InfoMessage:new{ text = text })
end

local function progress_message(text)
    local widget = InfoMessage:new{ text = text, timeout = 60 }
    UIManager:show(widget)
    if UIManager.forceRePaint then
        pcall(function() UIManager:forceRePaint() end)
    end
    return widget
end

local function close_progress(widget)
    if widget then
        pcall(function() UIManager:close(widget) end)
    end
    if UIManager.forceRePaint then
        pcall(function() UIManager:forceRePaint() end)
    end
end

local function after_progress(widget, callback)
    close_progress(widget)
    UIManager:nextTick(callback)
end

local function ensure_configured()
    if base_url() == "" then
        message(_("Set your Digest server URL first."))
        return false
    end
    if base_url():match("/trusted%-device/") or base_url():match("/kobo/") then
        message(_("Use the Digest site root as the server URL, not a trusted-device or Kobo URL."))
        return false
    end
    if api_token() == "" then
        message(_("Set a Digest API token first. Create one in Digest settings, then paste it here."))
        return false
    end
    if api_token():match("^dtd_") then
        message(_("That is a trusted-device token. Paste a Digest API token that starts with dgt_."))
        return false
    end
    return true
end

local function ensure_opds_configured()
    if not ensure_configured() then
        return false
    end
    if digest_username() == "" then
        message(_("Set your Digest username first. KOReader OPDS uses the username plus API token."))
        return false
    end
    return true
end

local function require_opds_browser()
    local ok, OPDSBrowser = pcall(require, "opdsbrowser")
    if ok and OPDSBrowser then
        return OPDSBrowser
    end
    ok, OPDSBrowser = pcall(require, "plugins/opds.koplugin/opdsbrowser")
    if ok and OPDSBrowser then
        return OPDSBrowser
    end
    return nil
end

local function http_client_for(url)
    if url:match("^https://") then
        return https_ok and https or nil
    end
    return http
end

function Digest:request(method, path, body, raw_sink)
    if not ensure_configured() then
        return nil
    end
    local request_url = join_url(path)
    local client = request_url and http_client_for(request_url)
    if not client then
        message(_("HTTPS is not available in this KOReader build."))
        return nil
    end

    local sink = {}
    local payload = body and JSON.encode(body) or nil
    local request = {
        url = request_url,
        method = method,
        headers = {
            ["Accept"] = raw_sink and "*/*" or "application/json",
            ["Accept-Encoding"] = "identity",
            ["Authorization"] = "Bearer " .. api_token(),
            ["User-Agent"] = "KOReader Digest plugin",
        },
        sink = raw_sink or ltn12.sink.table(sink),
    }
    if payload then
        request.headers["Content-Type"] = "application/json"
        request.headers["Content-Length"] = tostring(#payload)
        request.source = ltn12.source.string(payload)
    end

    socketutil:set_timeout(socketutil.LARGE_BLOCK_TIMEOUT, socketutil.LARGE_TOTAL_TIMEOUT)
    local code, headers, status = socket.skip(1, client.request(request))
    socketutil:reset_timeout()

    if not code then
        message(T(_("Cannot reach Digest.\n\n%1"), tostring(status or "network error")))
        return nil
    end
    if code == 401 then
        message(_(
            "Digest rejected the API token.\n\nCreate an API token in Digest admin tokens and paste the dgt_ token here. Do not use the Kobo endpoint token or a trusted-device link."
        ))
        return nil
    end
    if code < 200 or code >= 300 then
        message(T(_("Digest returned HTTP %1.\n\n%2"), tostring(code), tostring(status or "")))
        return nil
    end
    if raw_sink then
        return true, headers
    end

    local response = table.concat(sink)
    if response == "" then
        return {}
    end
    local ok, data = pcall(JSON.decode, response)
    if not ok or not data then
        message(_("Digest returned a response KOReader could not read."))
        return nil
    end
    return data
end

local function book_subtitle(book)
    local parts = {}
    local author = text_value(book.author)
    local series_value = text_value(book.series)
    local series_number = text_value(book.series_number)
    if author ~= "" then
        table.insert(parts, author)
    end
    if series_value ~= "" then
        if series_number ~= "" then
            series_value = series_value .. " #" .. series_number
        end
        table.insert(parts, series_value)
    end
    if book.in_library == false then
        table.insert(parts, _("Not in library"))
    end
    return table.concat(parts, " - ")
end

local function file_label(file)
    local size = number_value(file.size_bytes) or 0
    local format = text_value(file.format)
    local suffix = format ~= "" and format:upper() or _("File")
    if size > 0 then
        suffix = suffix .. " - " .. util.getFriendlySize(size)
    end
    return suffix
end

function Digest:showText(title, text)
    UIManager:show(TextViewer:new{
        title = title,
        title_multilines = true,
        text = text and text ~= "" and text or _("No description available."),
        text_type = "book_info",
    })
end

function Digest:addToEReader(book, file)
    local function first_dir(...)
        for i = 1, select("#", ...) do
            local value = select(i, ...)
            if type(value) == "string" and value ~= "" then
                return value
            end
        end
        return DataStorage:getDataDir()
    end
    local download_dir = first_dir(
        G_reader_settings:readSetting("download_dir"),
        G_reader_settings:readSetting("lastdir"),
        G_reader_settings:readSetting("home_dir"),
        Device and Device.home_dir)
    local title = text_value(book.title, "digest-" .. text_value(book.id, "book"))
    local extension = text_value(file.format, "epub"):lower()
    local filename = util.getSafeFilename(title, download_dir)
    if not filename:lower():match("%." .. extension .. "$") then
        filename = filename .. "." .. extension
    end
    local local_path = (download_dir ~= "/" and download_dir or "") .. "/" .. filename
    local handle = io.open(local_path, "wb")
    if not handle then
        message(T(_("Cannot write to %1"), BD.filepath(local_path)))
        return
    end

    local progress = progress_message(T(_("Downloading %1..."), title))
    local ok = self:request("GET", text_value(file.download_url), nil, ltn12.sink.file(handle))
    close_progress(progress)
    if ok then
        message(T(_("Download completed: %1"), BD.filepath(local_path)))
        if DocumentRegistry:hasProvider(local_path) and ReaderUI then
            ReaderUI:showReader(local_path)
        end
    else
        os.remove(local_path)
        message(_("Download failed."))
    end
end

function Digest:showBook(book_id, previous_loader)
    NetworkMgr:runWhenConnected(function()
        local book = self:request("GET", "/api/ereader/books/" .. encode(book_id))
        if not book then
            return
        end
        local items = {
            {
                text = _("Description"),
                mandatory = number_value(book.page_count) and text_value(book.page_count) .. "p" or nil,
                callback = function()
                    self:showText(text_value(book.title, _("Untitled")), text_value(book.description))
                end,
            },
        }
        for __, file in ipairs(table_value(book.files)) do
            table.insert(items, {
                text = T(_("Add to eReader %1"), file_label(file)),
                callback = function()
                    self:addToEReader(book, file)
                end,
            })
        end
        local author = text_value(book.author)
        if author ~= "" then
            table.insert(items, {
                text = _("More by this author"),
                callback = function()
                    self:showDiscoverAuthor(author)
                end,
            })
        end
        local reading = table_value(book.reading)
        local reading_state = text_value(reading.state)
        if reading_state ~= "" then
            table.insert(items, {
                text = _("Reading state"),
                mandatory = reading_state,
                callback = function()
                    message(
                        number_value(reading.progress_percent) and
                        T(_("Progress: %1%"), text_value(reading.progress_percent)) or reading_state
                    )
                end,
            })
        end
        if text_value(book.previous_id) ~= "" then
            table.insert(items, {
                text = _("Previous book"),
                callback = function()
                    self:showBook(text_value(book.previous_id), previous_loader)
                end,
            })
        end
        if text_value(book.next_id) ~= "" then
            table.insert(items, {
                text = _("Next book"),
                callback = function()
                    self:showBook(text_value(book.next_id), previous_loader)
                end,
            })
        end

        UIManager:show(Menu:new{
            title = text_value(book.title, _("Untitled")),
            subtitle = book_subtitle(book),
            item_table = items,
            items_max_lines = 3,
            onReturn = previous_loader,
        })
    end)
end

function Digest:addBookFileToEReader(book_id, file_id)
    NetworkMgr:runWhenConnected(function()
        local book = self:request("GET", "/api/ereader/books/" .. encode(book_id))
        if not book then
            return
        end
        local wanted = text_value(file_id)
        for __, file in ipairs(table_value(book.files)) do
            if text_value(file.id) == wanted then
                self:addToEReader(book, file)
                return
            end
        end
        message(_("Digest could not find that book file."))
    end)
end

function Digest:showDiscoveryBook(book, previous_loader)
    local items = {
        {
            text = _("Description"),
            callback = function()
                self:showText(text_value(book.title, _("Untitled")), text_value(book.description))
            end,
        },
    }
    if book.in_library and text_value(book.library_book_id) ~= "" then
        table.insert(items, {
            text = _("Open library copy"),
            callback = function()
                self:showBook(text_value(book.library_book_id), previous_loader)
            end,
        })
    else
        table.insert(items, {
            text = _("Request download"),
            callback = function()
                self:queueDownload(book)
            end,
        })
    end
    local author = text_value(book.author)
    if author ~= "" then
        table.insert(items, {
            text = _("More by this author"),
            callback = function()
                self:showDiscoverAuthor(author)
            end,
        })
    end
    UIManager:show(Menu:new{
        title = text_value(book.title, _("Untitled")),
        subtitle = book_subtitle(book),
        item_table = items,
        items_max_lines = 3,
        onReturn = previous_loader,
    })
end

function Digest:queueDownload(book)
    NetworkMgr:runWhenConnected(function()
        local progress = progress_message(_("Searching for releases..."))
        local data = self:request("POST", "/api/ereader/downloads", {
            source = text_value(book.source, "openlibrary"),
            source_id = text_value(book.source_id),
            title = text_value(book.title),
            author = text_value(book.author),
            isbn = text_value(book.isbn),
            cover_url = text_value(book.cover_url),
        })
        if not data then
            close_progress(progress)
            return
        end
        self:pollDownloadReleases(number_value(data.id), progress, 0)
    end)
end

function Digest:downloadById(wanted_id)
    if not wanted_id then
        return nil
    end
    local data = self:request("GET", "/api/ereader/downloads")
    if not data then
        return nil
    end
    for __, item in ipairs(table_value(data.items)) do
        if number_value(item.id) == wanted_id then
            return item
        end
    end
    return nil
end

function Digest:isDownloadTerminal(item)
    local status = text_value(item and item.status)
    return status == "available" or status == "failed" or status == "cancelled"
end

function Digest:pollDownloadReleases(wanted_id, progress, attempt)
    if not wanted_id then
        close_progress(progress)
        message(_("Download request failed."))
        return
    end
    UIManager:scheduleIn(attempt == 0 and 0.5 or 2, function()
        local item = self:downloadById(wanted_id)
        if not item then
            close_progress(progress)
            message(_("Download request could not be found."))
            return
        end
        local releases = table_value(item.releases)
        local status = text_value(item.status)
        if #releases > 0 and status == "wanted" then
            after_progress(progress, function()
                self:showReleaseSelector(item)
            end)
            return
        end
        if status == "available" then
            close_progress(progress)
            message(_("Download completed."))
            return
        end
        if status == "failed" or status == "cancelled" then
            close_progress(progress)
            local err = text_value(item.last_error)
            if err ~= "" then
                message(T(_("No releases found.\n\n%1"), err))
            else
                message(_("No releases found."))
            end
            return
        end
        if attempt >= 30 then
            close_progress(progress)
            message(_("Still searching for releases. Check Downloads for updates."))
            return
        end
        self:pollDownloadReleases(wanted_id, progress, attempt + 1)
    end)
end

function Digest:showReleaseSelector(item)
    local releases = table_value(item.releases)
    if #releases == 0 then
        message(_("No releases found."))
        return
    end
    local menu
    local rows = {}
    for __, release in ipairs(releases) do
        table.insert(rows, {
            text = text_value(release.title, _("Untitled")),
            mandatory = file_label(release),
            callback = function()
                UIManager:close(menu)
                self:chooseRelease(item, release)
            end,
        })
    end
    table.insert(rows, {
        text = _("Cancel"),
        callback = function()
            UIManager:close(menu)
        end,
    })
    menu = Menu:new{
        title = _("Select release"),
        subtitle = text_value(item.title, _("Untitled")),
        item_table = rows,
        items_max_lines = 3,
    }
    UIManager:show(menu)
end

function Digest:chooseRelease(item, release)
    NetworkMgr:runWhenConnected(function()
        local progress = progress_message(T(_("Downloading %1...\nStatus: starting"), text_value(item.title, _("book"))))
        local data = self:request(
            "POST",
            "/api/ereader/downloads/" .. tostring(item.id) .. "/releases/" .. tostring(release.id)
        )
        if not data then
            close_progress(progress)
            message(_("Download failed."))
            return
        end
        self:pollDownloadProgress(number_value(item.id), progress, nil, 0)
    end)
end

function Digest:pollDownloadProgress(wanted_id, progress, last_status, attempt)
    UIManager:scheduleIn(attempt == 0 and 0.5 or 2, function()
        local item = self:downloadById(wanted_id)
        if not item then
            close_progress(progress)
            message(_("Download failed."))
            return
        end
        local status = text_value(item.status, _("unknown"))
        if status ~= last_status then
            close_progress(progress)
            progress = progress_message(T(
                _("Downloading %1...\nStatus: %2"),
                text_value(item.title, _("book")),
                status
            ))
        end
        if status == "available" then
            close_progress(progress)
            message(_("Download completed."))
            return
        end
        if status == "failed" or status == "cancelled" then
            close_progress(progress)
            local err = text_value(item.last_error)
            if err ~= "" then
                message(T(_("Download failed.\n\n%1"), err))
            else
                message(_("Download failed."))
            end
            return
        end
        if attempt >= 120 then
            close_progress(progress)
            message(_("Download is still running. Check Downloads for updates."))
            return
        end
        self:pollDownloadProgress(wanted_id, progress, status, attempt + 1)
    end)
end

function Digest:bookItems(data, previous_loader)
    local items = {}
    for __, book in ipairs(table_value(data.items)) do
        table.insert(items, {
            text = text_value(book.title, _("Untitled")),
            mandatory = book_subtitle(book),
            callback = function()
                local book_id = text_value(book.id)
                if book_id ~= "" then
                    self:showBook(book_id, previous_loader)
                else
                    self:showDiscoveryBook(book, previous_loader)
                end
            end,
        })
    end
    if #items == 0 then
        table.insert(items, { text = _("No books found.") })
    end
    return items
end

function Digest:showBookList(title, path, page, previous_loader)
    page = page or 1
    NetworkMgr:runWhenConnected(function()
        local separator = path:find("?", 1, true) and "&" or "?"
        local data = self:request("GET", path .. separator .. "page_size=24&page=" .. tostring(page))
        if not data then
            return
        end
        local loader = function()
            self:showBookList(title, path, page, previous_loader)
        end
        local items = self:bookItems(data, loader)
        if page > 1 then
            table.insert(items, 1, {
                text = _("Previous page"),
                callback = function()
                    self:showBookList(title, path, page - 1, previous_loader)
                end,
            })
        end
        if data.has_more == true then
            table.insert(items, {
                text = _("Next page"),
                callback = function()
                    self:showBookList(title, path, page + 1, previous_loader)
                end,
            })
        end
        UIManager:show(Menu:new{
            title = title,
            subtitle = number_value(data.total) and T(_("%1 books"), text_value(data.total)) or nil,
            item_table = items,
            items_max_lines = 2,
            onReturn = previous_loader,
        })
    end)
end

function Digest:opdsServer(title, path)
    return {
        title = title,
        url = join_url(path),
        username = digest_username(),
        password = api_token(),
        raw_names = true,
    }
end

function Digest:openOPDSCatalog(title, path, fallback)
    if not ensure_opds_configured() then
        return
    end
    local catalog_url = join_url(path)
    local OPDSBrowser = require_opds_browser()
    if not OPDSBrowser then
        if fallback then
            message(_("KOReader OPDS browser was not found. Opening Digest's native fallback view."))
            fallback()
        else
            message(_("KOReader OPDS browser was not found."))
        end
        return
    end
    local browser
    browser = OPDSBrowser:new{
        settings = {},
        title = title,
        servers = { self:opdsServer(title, path) },
        downloads = {},
        pending_syncs = {},
        is_popout = false,
        is_borderless = true,
        title_bar_fm_style = true,
        _manager = { updated = false, ui = self.ui },
        close_callback = function()
            if browser.download_list then
                browser.download_list.close_callback()
            end
            UIManager:close(browser)
        end,
        file_downloaded_callback = function(file)
            message(T(_("Added to eReader: %1"), BD.filepath(file)))
        end,
        file_read_now_callback = function(file)
            if ReaderUI then
                ReaderUI:showReader(file)
            elseif self.ui and self.ui.openFile then
                self.ui:openFile(file)
            end
        end,
    }
    UIManager:show(browser)
    browser.root_catalog_title = title
    browser.root_catalog_username = digest_username()
    browser.root_catalog_password = api_token()
    browser.root_catalog_raw_names = true
    browser.catalog_title = title
    NetworkMgr:runWhenConnected(function()
        browser:updateCatalog(catalog_url)
    end)
end

function Digest:installOPDSCatalogs()
    if not ensure_opds_configured() then
        return
    end
    local servers = opds_settings:readSetting("servers", default_opds_servers)
    local filtered = {}
    for __, server in ipairs(table_value(servers)) do
        if server.title ~= "Digest Library"
            and server.title ~= "Digest Library - By Title"
            and server.title ~= "Digest Library - By Author"
            and server.title ~= "Digest Library - Latest"
            and server.title ~= "Digest Library - By Series"
            and server.title ~= "Digest Discover"
            and server.title ~= "Digest Discover - Search"
            and server.title ~= "Digest Discover - Trending"
            and server.title ~= "Digest Discover - NYT Bestsellers"
            and server.title ~= "Digest Discover - New Releases"
            and server.title ~= "Digest Downloads" then
            table.insert(filtered, server)
        end
    end
    table.insert(filtered, self:opdsServer("Digest Library", "/opds"))
    table.insert(filtered, self:opdsServer("Digest Library - By Title", "/opds/catalog/title"))
    table.insert(filtered, self:opdsServer("Digest Library - By Author", "/opds/catalog/authors-v2"))
    table.insert(filtered, self:opdsServer("Digest Library - Latest", "/opds/catalog/latest"))
    table.insert(filtered, self:opdsServer("Digest Library - By Series", "/opds/catalog/series"))
    table.insert(filtered, self:opdsServer("Digest Discover - Search", "/opds/discover/search"))
    table.insert(filtered, self:opdsServer("Digest Discover - Trending", "/opds/discover/trending"))
    table.insert(filtered, self:opdsServer("Digest Discover - NYT Bestsellers", "/opds/discover/nyt-bestsellers"))
    table.insert(filtered, self:opdsServer("Digest Discover - New Releases", "/opds/discover/new-releases"))
    table.insert(filtered, self:opdsServer("Digest Downloads", "/opds/downloads"))
    opds_settings:saveSetting("servers", filtered)
    opds_settings:flush()

    local opds = self.ui and self.ui.opds
    if opds then
        if opds.loadSettings then
            opds:loadSettings()
        end
        opds.servers = filtered
        opds.updated = true
        if opds.settings then
            opds.settings:saveSetting("servers", filtered)
            opds.settings:flush()
        end
    end

    message(T(
        _("Added Digest OPDS catalogs by Title, Author, Latest, Series, Search, Trending, NYT Bestsellers, New Releases, and Downloads.\n\nTotal OPDS catalogs: %1"),
        tostring(#filtered)
    ))
end

function Digest:showLibraryNative(view)
    view = view or "latest"
    self:showBookList(_("Digest Library"), "/api/ereader/library?view=" .. encode(view), 1)
end

function Digest:showLibrary(view)
    view = view or "latest"
    if view == "latest" or view == "all" then
        self:openOPDSCatalog(_("Digest Library"), "/opds", function()
            self:showLibraryNative(view)
        end)
    else
        self:showLibraryNative(view)
    end
end

function Digest:showDiscoverGroup(title, path)
    NetworkMgr:runWhenConnected(function()
        local data = self:request("GET", path)
        if not data then
            return
        end
        UIManager:show(Menu:new{
            title = title,
            item_table = self:bookItems(data, function()
                self:showDiscoverGroup(title, path)
            end),
            items_max_lines = 2,
        })
    end)
end

function Digest:showDiscover()
    self:openOPDSCatalog(_("Digest Discover"), "/opds/discover", function()
        self:showDiscoverNative()
    end)
end

function Digest:showDiscoverDownloadStatus(item)
    local lines = {
        T(_("Title: %1"), text_value(item.title, _("Untitled"))),
        T(_("Author: %1"), text_value(item.author, _("Unknown"))),
        T(_("Status: %1"), text_value(item.status, _("unknown"))),
    }
    if text_value(item.last_error) ~= "" then
        table.insert(lines, "")
        table.insert(lines, T(_("Last error: %1"), text_value(item.last_error)))
    end
    local releases = table_value(item.releases)
    if #releases > 0 then
        table.insert(lines, "")
        table.insert(lines, _("Available releases:"))
        for __, release in ipairs(releases) do
            table.insert(lines, text_value(release.title, _("Untitled")) .. " - " .. file_label(release))
        end
    end
    self:showText(text_value(item.title, _("Download status")), table.concat(lines, "\n"))
end

function Digest:showDiscoverSearch()
    NetworkMgr:runWhenConnected(function()
        local data = self:request("GET", "/api/ereader/downloads")
        if not data then
            return
        end
        local items = {
            {
                text = _("New search"),
                callback = function()
                    self:promptSearch("discover")
                end,
            },
        }
        for __, item in ipairs(table_value(data.items)) do
            table.insert(items, {
                text = text_value(item.title, _("Untitled")),
                mandatory = T(_("Status: %1"), text_value(item.status, _("unknown"))),
                callback = function()
                    if text_value(item.acquired_book_id) ~= "" then
                        self:showBook(text_value(item.acquired_book_id), function()
                            self:showDiscoverSearch()
                        end)
                    else
                        self:showDiscoverDownloadStatus(item)
                    end
                end,
            })
        end
        UIManager:show(Menu:new{
            title = _("Digest Discover - Search"),
            item_table = items,
            items_max_lines = 2,
        })
    end)
end

function Digest:showDiscoverGenreCatalog(title, next_callback)
    local items = {}
    for __, genre in ipairs(discover_genres) do
        table.insert(items, {
            text = genre.label,
            callback = function()
                next_callback(genre)
            end,
        })
    end
    UIManager:show(Menu:new{
        title = title,
        item_table = items,
        items_max_lines = 2,
    })
end

function Digest:showDiscoverPeriodCatalog(title, genre, periods, next_callback)
    local items = {}
    for __, period in ipairs(periods) do
        table.insert(items, {
            text = period.label,
            callback = function()
                next_callback(genre, period)
            end,
        })
    end
    UIManager:show(Menu:new{
        title = title,
        subtitle = genre.label,
        item_table = items,
        items_max_lines = 2,
    })
end

function Digest:showDiscoverTrending()
    self:showDiscoverGenreCatalog(_("Digest Discover - Trending"), function(genre)
        self:showDiscoverPeriodCatalog(_("Trending"), genre, trending_periods, function(selected_genre, period)
            self:showDiscoverGroup(
                _("Trending") .. " - " .. selected_genre.label .. " - " .. period.label,
                "/api/ereader/discover/trending?genre=" .. encode(selected_genre.provider) ..
                    "&period=" .. encode(period.key)
            )
        end)
    end)
end

function Digest:showDiscoverNewReleases()
    self:showDiscoverGenreCatalog(_("Digest Discover - New Releases"), function(genre)
        self:showDiscoverPeriodCatalog(_("New Releases"), genre, new_release_periods, function(selected_genre, period)
            self:showDiscoverGroup(
                _("New Releases") .. " - " .. selected_genre.label .. " - " .. period.label,
                "/api/ereader/discover/new-releases?genre=" .. encode(selected_genre.provider) ..
                    "&period=" .. encode(period.key)
            )
        end)
    end)
end

function Digest:showDiscoverNytWeeks(list_item)
    NetworkMgr:runWhenConnected(function()
        local data = self:request(
            "GET",
            "/api/ereader/discover/bestsellers/weeks?slug=" .. encode(text_value(list_item.slug))
        )
        if not data then
            return
        end
        local items = {}
        for __, week in ipairs(table_value(data.items)) do
            table.insert(items, {
                text = text_value(week.title, text_value(week.date)),
                callback = function()
                    self:showDiscoverGroup(
                        _("NYT Bestsellers") .. " - " ..
                            text_value(list_item.title) .. " - " .. text_value(week.title),
                        "/api/ereader/discover/bestsellers?slug=" .. encode(text_value(list_item.slug)) ..
                            "&week=" .. encode(text_value(week.date))
                    )
                end,
            })
        end
        if #items == 0 then
            table.insert(items, { text = _("No bestseller weeks found.") })
        end
        UIManager:show(Menu:new{
            title = T(_("NYT Bestsellers - %1"), text_value(list_item.title)),
            item_table = items,
            items_max_lines = 2,
        })
    end)
end

function Digest:showDiscoverNytBestsellers()
    NetworkMgr:runWhenConnected(function()
        local data = self:request("GET", "/api/ereader/discover/bestsellers/lists")
        if not data then
            return
        end
        local items = {}
        for __, item in ipairs(table_value(data.items)) do
            table.insert(items, {
                text = text_value(item.title, text_value(item.slug)),
                callback = function()
                    self:showDiscoverNytWeeks(item)
                end,
            })
        end
        if #items == 0 then
            table.insert(items, { text = _("No NYT bestseller lists found.") })
        end
        UIManager:show(Menu:new{
            title = _("Digest Discover - NYT Bestsellers"),
            item_table = items,
            items_max_lines = 2,
        })
    end)
end

function Digest:showDiscoverNative()
    UIManager:show(Menu:new{
        title = _("Digest Discover"),
        item_table = {
            {
                text = _("Search"),
                callback = function()
                    self:showDiscoverSearch()
                end,
            },
            {
                text = _("Trending"),
                callback = function()
                    self:showDiscoverTrending()
                end,
            },
            {
                text = _("NYT Bestsellers"),
                callback = function()
                    self:showDiscoverNytBestsellers()
                end,
            },
            {
                text = _("New Releases"),
                callback = function()
                    self:showDiscoverNewReleases()
                end,
            },
        },
    })
end

function Digest:showDiscoverAuthor(author)
    self:showDiscoverGroup(
        T(_("More by %1"), author),
        "/api/ereader/discover/author?author=" .. encode(author)
    )
end

function Digest:promptSearch(scope)
    local dialog
    dialog = InputDialog:new{
        title = scope == "discover" and _("Search Discover") or _("Search Library"),
        input = "",
        buttons = {{
            {
                text = _("Cancel"),
                callback = function()
                    UIManager:close(dialog)
                end,
            },
            {
                text = _("Search"),
                is_enter_default = true,
                callback = function()
                    local query = trim(dialog:getInputText())
                    UIManager:close(dialog)
                    if query == "" then
                        return
                    end
                    if scope == "discover" then
                        self:showDiscoverGroup(
                            _("Discover search"),
                            "/api/ereader/discover/search?q=" .. encode(query)
                        )
                    else
                        self:showBookList(
                            _("Library search"),
                            "/api/ereader/library?q=" .. encode(query),
                            1
                        )
                    end
                end,
            },
        }},
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function Digest:promptSetting(key, title, hint)
    local dialog
    dialog = InputDialog:new{
        title = title,
        input = settings:readSetting(key) or "",
        input_hint = hint,
        text_type = key == "api_token" and "password" or nil,
        buttons = {{
            {
                text = _("Cancel"),
                callback = function()
                    UIManager:close(dialog)
                end,
            },
            {
                text = _("Save"),
                is_enter_default = true,
                callback = function()
                    settings:saveSetting(key, trim(dialog:getInputText()))
                    if key == "api_token" then
                        settings:saveSetting(key, api_token())
                    end
                    settings:flush()
                    UIManager:close(dialog)
                end,
            },
        }},
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function Digest:testConnection()
    NetworkMgr:runWhenConnected(function()
        local data = self:request("GET", "/api/ereader/library?page_size=1&page=1")
        if data then
            message(T(_("Connected to Digest. %1 books available."), text_value(data.total, "0")))
        end
    end)
end

function Digest:showNativeLink(target)
    target = trim(target)
    local route = target:lower()
    if route == "" or route == "library" or route == "home" then
        self:showLibrary()
    elseif route == "discover" then
        self:showDiscover()
    elseif route:match("^books/[^/]+/file/%d+$") then
        local book_id, file_id = target:match("^books/([^/]+)/file/(%d+)$")
        self:addBookFileToEReader(socketurl.unescape(book_id:gsub("+", " ")), file_id)
    elseif route:match("^library%?") then
        local view = route:match("[?&]view=([^&]+)") or "latest"
        self:showLibrary(view)
    elseif route:match("^discover/author%?") then
        local author = target:match("[?&]author=([^&]+)")
        self:showDiscoverAuthor(author and socketurl.unescape(author:gsub("+", " ")) or "")
    else
        message(T(_("Digest link is not supported yet:\n\n%1"), target))
    end
end

local function target_from_digest_link(link_url)
    return link_url:match("^digest://(.*)$") or link_url:match("^digest:(.*)$")
end

local function patch_readerlink(plugin)
    if patched_readerlink then
        return
    end
    local ok, ReaderLink = pcall(require, "apps/reader/modules/readerlink")
    if not ok or not ReaderLink then
        return
    end

    local init_orig = ReaderLink.init
    ReaderLink.init = function(readerlink)
        init_orig(readerlink)
        readerlink:registerScheme("digest")
    end

    local external_orig = ReaderLink.onGoToExternalLink
    ReaderLink.onGoToExternalLink = function(readerlink, link_url)
        local target = target_from_digest_link(link_url)
        if target then
            plugin:showNativeLink(target)
            return true
        end
        return external_orig(readerlink, link_url)
    end

    patched_readerlink = true
end

function Digest:addToMainMenu(menu_items)
    menu_items.digest = {
        text = _("Digest"),
        sorting_hint = "more_tools",
        sub_item_table = {
            {
                text = _("Library"),
                callback = function()
                    self:showLibrary()
                end,
            },
            {
                text = _("Discover"),
                callback = function()
                    self:showDiscover()
                end,
            },
            {
                text = _("Search library"),
                callback = function()
                    self:promptSearch("library")
                end,
            },
            {
                text = _("Search discover"),
                callback = function()
                    self:promptSearch("discover")
                end,
            },
            {
                text = _("Install OPDS catalogs"),
                callback = function()
                    self:installOPDSCatalogs()
                end,
            },
            {
                text = _("Native library fallback"),
                callback = function()
                    self:showLibraryNative()
                end,
            },
            {
                text = _("Native discover fallback"),
                callback = function()
                    self:showDiscoverNative()
                end,
            },
            {
                text = _("Set server URL"),
                keep_menu_open = true,
                callback = function()
                    self:promptSetting("server_url", _("Digest server URL"), "https://digest.example")
                end,
            },
            {
                text = _("Set username"),
                keep_menu_open = true,
                callback = function()
                    self:promptSetting("username", _("Digest username"), "admin")
                end,
            },
            {
                text = _("Set API token"),
                keep_menu_open = true,
                callback = function()
                    self:promptSetting("api_token", _("Digest API token"), "dgt_...")
                end,
            },
            {
                text = _("Test connection"),
                callback = function()
                    self:testConnection()
                end,
            },
            {
                text = _("Link examples"),
                callback = function()
                    message(_("Use these links in EPUBs or notes:\n\ndigest://library\ndigest://discover"))
                end,
            },
        },
    }
end

function Digest:init()
    patch_readerlink(self)
    if self.ui and self.ui.menu then
        self.ui.menu:registerToMainMenu(self)
    end
end

function Digest:onReaderReady()
    if self.ui and self.ui.link and self.ui.link.registerScheme then
        self.ui.link:registerScheme("digest")
    end
end

return Digest
