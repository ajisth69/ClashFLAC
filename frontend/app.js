const isLocalDev = typeof window !== "undefined" && (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1");
const DEFAULT_API_BASE = isLocalDev ? (window.location.port === "5173" ? "" : "http://127.0.0.1:8000") : "https://clashflac-production.up.railway.app";
const PREVIEW_API = "https://jiosavan.clashgram.workers.dev/api";

const savedApi = localStorage.getItem("clash-api-base");
const activeApi = isLocalDev
  ? DEFAULT_API_BASE
  : ((!savedApi || savedApi === "https://clashflac.up.railway.app" || savedApi.startsWith("https://clashflac.up.railway.app") || savedApi.includes("localhost") || savedApi.includes("127.0.0.1") || savedApi.includes(":8787") || savedApi.includes(":8788")) ? DEFAULT_API_BASE : savedApi);

const state = {
    apiBase: activeApi,
    quality: localStorage.getItem("clash-quality") || "UHD",
    enginePriority: localStorage.getItem("clash-engine-priority") || "amazon",
    embedArt: localStorage.getItem("clash-embed-art") !== "false",
    embedLyrics: localStorage.getItem("clash-embed-lyrics") !== "false",
    theme: localStorage.getItem("clash-theme") || "light",
    results: [],
    filteredResults: [],
    selectedTrack: null,
    currentTrack: null,
    context: [],
    contextIndex: -1,
    queue: [],
    history: [],
    downloads: [],
    viewMode: localStorage.getItem("clash-view") || "list",
    repeatMode: "off",
    shuffle: false,
    searchController: null,
    searchMode: "music",
    recentSearches: readJson("clash-recent-searches", []),
    turnstileSiteKey: localStorage.getItem("clash-turnstile-sitekey") || "0x4AAAAAAEPBXfH8QLJ1Sekq",
    turnstileToken: "",
    turnstileWidgetId: null,
};

const dom = {};

function icon(name, className = "") {
    return `<svg${className ? ` class="${className}"` : ""} aria-hidden="true"><use href="#icon-${name}"></use></svg>`;
}

function escapeHtml(value = "") {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function readJson(key, fallback) {
    try {
        const parsed = JSON.parse(localStorage.getItem(key));
        return parsed ?? fallback;
    } catch {
        return fallback;
    }
}

function safeUrl(value) {
    try {
        const url = new URL(value);
        return ["http:", "https:"].includes(url.protocol) ? url.href : "";
    } catch {
        return "";
    }
}

function normalize(value = "") {
    return String(value)
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "")
        .toLowerCase()
        .replace(/&amp;/g, "and")
        .replace(/[^a-z0-9]+/g, " ")
        .trim();
}

function decodeEntities(value = "") {
    const area = document.createElement("textarea");
    area.innerHTML = value;
    return area.value;
}

function formatDuration(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value <= 0) return "—";
    const minutes = Math.floor(value / 60);
    const remainder = Math.floor(value % 60);
    return `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function formatBitrate(value) {
    const bitrate = Number(value);
    if (!bitrate) return "Best available";
    return `${Math.round(bitrate / 1000).toLocaleString()} kbps`;
}

function fileSafe(value) {
    return String(value || "track").replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_").trim();
}

function api(path) {
    if (!state.apiBase) return path;
    return `${state.apiBase.replace(/\/$/, "")}${path}`;
}

function getPreviewUrl(item) {
    const options = item?.downloadUrl || [];
    if (!Array.isArray(options) || !options.length) return "";
    const preferred = options.find((entry) => entry.quality === "320kbps") || options.at(-1);
    return safeUrl(preferred?.url);
}

function upgradeThumbnail(url) {
    if (!url) return "";
    let clean = safeUrl(url);
    if (!clean) return "";
    if (clean.includes("._S")) {
        clean = clean.replace(/\._[A-Z0-9_,]+_\./i, "._SL1400_.");
    }
    if (clean.includes("mzstatic.com")) {
        clean = clean.replace(/\/\d+x\d+bb\./i, "/1400x1400bb.");
    }
    if (clean.includes("resources.tidal.com")) {
        clean = clean.replace(/\/\d+x\d+\.jpg$/i, "/1280x1280.jpg");
    }
    if (clean.includes("saavncdn.com")) {
        clean = clean.replace(/\d+x\d+/, "500x500");
    }
    return clean;
}

function getImage(item, preferred = "500x500") {
    const images = item?.image || [];
    if (!Array.isArray(images) || !images.length) return "";
    return safeUrl(
        images.find((entry) => entry.quality === preferred)?.url ||
        images.find((entry) => entry.quality === "500x500")?.url ||
        images.find((entry) => entry.quality === "150x150")?.url ||
        images.at(-1)?.url
    );
}

function imageMarkup(url, alt, className = "") {
    const source = safeUrl(url);
    return source ? `<img src="${escapeHtml(source)}" alt="${escapeHtml(alt)}" class="${className}" loading="lazy">` : icon("music");
}

function getActiveSiteKey() {
    return state.turnstileSiteKey || "0x4AAAAAAEPBXfH8QLJ1Sekq";
}

function initTurnstile() {
    if (!window.turnstile) return;
    const container = document.getElementById("cf-turnstile-container");
    if (!container || state.turnstileWidgetId !== null) return;
    try {
        state.turnstileWidgetId = window.turnstile.render(container, {
            sitekey: getActiveSiteKey(),
            callback: (token) => {
                state.turnstileToken = token;
                if (state.turnstileResolver) {
                    state.turnstileResolver(token);
                    state.turnstileResolver = null;
                }
            },
            "expired-callback": () => {
                state.turnstileToken = "";
                if (state.turnstileWidgetId !== null) {
                    window.turnstile.reset(state.turnstileWidgetId);
                }
            },
            "error-callback": () => {
                state.turnstileToken = "";
                if (state.turnstileResolver) {
                    state.turnstileResolver("");
                    state.turnstileResolver = null;
                }
            },
            size: "invisible",
        });
    } catch (err) {
        console.warn("Turnstile init notice:", err);
    }
}

// Global hook for Turnstile script onload
window.onTurnstileLoaded = () => {
    initTurnstile();
};

async function getTurnstileToken() {
    if (state.turnstileToken) {
        const t = state.turnstileToken;
        return t;
    }
    if (!window.turnstile) return "";

    initTurnstile();

    return new Promise((resolve) => {
        state.turnstileResolver = resolve;
        try {
            if (state.turnstileWidgetId !== null) {
                window.turnstile.execute(state.turnstileWidgetId);
            } else {
                const container = document.getElementById("cf-turnstile-container");
                if (container) window.turnstile.execute(container);
            }
        } catch (e) {
            console.warn("Turnstile execute:", e);
        }
        // Instant fallback resolve after 1.2s so downloads never lag or fail
        setTimeout(() => {
            if (state.turnstileResolver) {
                state.turnstileResolver(state.turnstileToken || "");
                state.turnstileResolver = null;
            }
        }, 1200);
    });
}

function resetTurnstile() {
    if (window.turnstile && state.turnstileWidgetId !== null) {
        try {
            window.turnstile.reset(state.turnstileWidgetId);
        } catch {}
    }
    state.turnstileToken = "";
    state.turnstileResolver = null;
}

async function requestJson(url, options = {}) {
    const headers = options.headers ? new Headers(options.headers) : new Headers();
    const response = await fetch(url, { ...options, headers });
    if (!response.ok) {
        let detail = `Request failed (${response.status})`;
        try {
            const body = await response.json();
            detail = body.detail || body.message || detail;
        } catch {
            // Keep the HTTP fallback message.
        }
        throw new Error(detail);
    }
    return response.json();
}

function initialize() {
    Object.assign(dom, {
        searchForm: document.getElementById("search-form"),
        searchInput: document.getElementById("search-input"),
        searchResults: document.getElementById("search-results"),
        resultsTitle: document.getElementById("results-title"),
        resultsSummary: document.getElementById("results-summary"),
        toggleFilters: document.getElementById("toggle-filters"),
        metadataFilters: document.getElementById("metadata-filters"),
        artistFilter: document.getElementById("artist-filter"),
        albumFilter: document.getElementById("album-filter"),
        yearFilter: document.getElementById("year-filter"),
        durationFilter: document.getElementById("duration-filter"),
        searchScope: document.getElementById("search-scope"),
        availabilityFilter: document.getElementById("availability-filter"),
        clearFilters: document.getElementById("clear-filters"),
        searchModeHint: document.getElementById("search-mode-hint"),
        sortResults: document.getElementById("sort-results"),
        recentRow: document.getElementById("recent-row"),
        recentSearches: document.getElementById("recent-searches"),
        clearRecents: document.getElementById("clear-recents"),
        inspector: document.getElementById("track-inspector"),
        queueList: document.getElementById("play-queue"),
        downloadList: document.getElementById("download-list"),
        downloadCounter: document.getElementById("download-counter"),
        navDownloadCount: document.getElementById("nav-download-count"),
        navQueueCount: document.getElementById("nav-queue-count"),
        clearQueue: document.getElementById("clear-queue"),
        audio: document.getElementById("audio-element"),
        playerCover: document.getElementById("player-cover"),
        playerTitle: document.getElementById("player-title"),
        playerArtist: document.getElementById("player-artist"),
        playButton: document.getElementById("play-button"),
        playButtonIcon: document.getElementById("play-button-icon"),
        previousButton: document.getElementById("previous-button"),
        nextButton: document.getElementById("next-button"),
        shuffleButton: document.getElementById("shuffle-button"),
        repeatButton: document.getElementById("repeat-button"),
        seekSlider: document.getElementById("seek-slider"),
        currentTime: document.getElementById("current-time"),
        totalTime: document.getElementById("total-time"),
        volumeSlider: document.getElementById("volume-slider"),
        downloadCurrent: document.getElementById("download-current"),
        playerSheet: document.getElementById("player-sheet"),
        sheetCover: document.getElementById("sheet-cover"),
        sheetTitle: document.getElementById("sheet-title"),
        sheetArtist: document.getElementById("sheet-artist"),
        sheetAlbum: document.getElementById("sheet-album"),
        sheetPlay: document.getElementById("sheet-play"),
        sheetPlayIcon: document.getElementById("sheet-play-icon"),
        sheetPrevious: document.getElementById("sheet-previous"),
        sheetNext: document.getElementById("sheet-next"),
        sheetSeek: document.getElementById("sheet-seek-slider"),
        sheetCurrentTime: document.getElementById("sheet-current-time"),
        sheetTotalTime: document.getElementById("sheet-total-time"),
        sheetDownload: document.getElementById("sheet-download"),
        themeToggle: document.getElementById("theme-toggle"),
        themeIcon: document.getElementById("theme-icon"),
        floatingDownload: document.getElementById("floating-download-indicator"),
        floatingDownloadTitle: document.getElementById("floating-download-title"),
        floatingDownloadDesc: document.getElementById("floating-download-desc"),
        settingsDialog: document.getElementById("settings-dialog"),
        settingsForm: document.getElementById("settings-form"),
        apiBaseInput: document.getElementById("api-base-input"),
        qualitySetting: document.getElementById("quality-setting"),
        toastRegion: document.getElementById("toast-region"),
    });

    applyTheme(state.theme);
    applyViewMode(state.viewMode);
    renderRecents();
    bindEvents();
    setTimeout(initTurnstile, 400);

    const savedVolume = Number(localStorage.getItem("clash-volume") ?? 80);
    dom.audio.volume = Math.min(1, Math.max(0, savedVolume / 100));
    dom.volumeSlider.value = String(savedVolume);
    updateRangeFill(dom.volumeSlider, savedVolume);
    checkConnection();

    const initialHash = window.location.hash.replace("#", "");
    if (["discover", "downloads", "queue"].includes(initialHash)) {
        setActiveNav(initialHash, true);
    }
}

function bindEvents() {
    dom.searchForm.addEventListener("submit", handleSearch);
    dom.toggleFilters?.addEventListener("click", () => {
        const willOpen = dom.metadataFilters.hidden;
        dom.metadataFilters.hidden = !willOpen;
        dom.toggleFilters.setAttribute("aria-expanded", String(willOpen));
    });

    [dom.artistFilter, dom.albumFilter, dom.yearFilter].filter(Boolean).forEach((input) => input.addEventListener("input", applyFilters));
    [dom.durationFilter, dom.searchScope, dom.availabilityFilter, dom.sortResults].filter(Boolean).forEach((input) => input.addEventListener("change", applyFilters));
    dom.clearFilters?.addEventListener("click", clearMetadataFilters);
    document.querySelectorAll("[data-search-mode]").forEach((button) => button.addEventListener("click", () => setSearchMode(button.dataset.searchMode)));

    document.querySelectorAll("[data-example]").forEach((button) => button.addEventListener("click", () => runExampleSearch(button.dataset.example)));
    document.querySelectorAll("[data-view-mode]").forEach((button) => button.addEventListener("click", () => applyViewMode(button.dataset.viewMode)));

    dom.recentSearches?.addEventListener("click", (event) => {
        const chip = event.target.closest("[data-recent]");
        if (chip) runExampleSearch(chip.dataset.recent);
    });
    dom.clearRecents?.addEventListener("click", () => {
        state.recentSearches = [];
        localStorage.removeItem("clash-recent-searches");
        renderRecents();
    });

    dom.searchResults.addEventListener("click", handleResultAction);
    dom.searchResults.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        const card = event.target.closest("[data-track-id]");
        if (!card || event.target.closest("button")) return;
        event.preventDefault();
        const track = state.filteredResults.find((item) => item.id === card.dataset.trackId);
        if (track) selectTrack(track);
    });
    dom.inspector?.addEventListener("click", handleInspectorAction);
    dom.queueList.addEventListener("click", handleQueueAction);
    dom.downloadList.addEventListener("click", handleDownloadAction);
    dom.clearQueue.addEventListener("click", clearPlayQueue);

    dom.playButton.addEventListener("click", togglePlayback);
    dom.previousButton.addEventListener("click", playPrevious);
    dom.nextButton.addEventListener("click", () => playNext(false));
    dom.shuffleButton.addEventListener("click", toggleShuffle);
    dom.repeatButton.addEventListener("click", cycleRepeat);
    dom.seekSlider.addEventListener("input", () => seekFrom(dom.seekSlider));
    dom.volumeSlider.addEventListener("input", handleVolume);
    dom.downloadCurrent.addEventListener("click", () => state.currentTrack && startDownload(state.currentTrack));

    dom.sheetPlay.addEventListener("click", togglePlayback);
    dom.sheetPrevious.addEventListener("click", playPrevious);
    dom.sheetNext.addEventListener("click", () => playNext(false));
    dom.sheetSeek.addEventListener("input", () => seekFrom(dom.sheetSeek));
    dom.sheetDownload.addEventListener("click", () => state.currentTrack && startDownload(state.currentTrack));
    document.getElementById("open-player-sheet").addEventListener("click", openPlayerSheet);
    document.querySelector(".player-track-copy")?.addEventListener("click", openPlayerSheet);
    document.getElementById("expand-player").addEventListener("click", openPlayerSheet);
    document.querySelectorAll("[data-close-sheet]").forEach((button) => button.addEventListener("click", closePlayerSheet));

    dom.audio.addEventListener("timeupdate", syncTimeline);
    dom.audio.addEventListener("loadedmetadata", syncTimeline);
    dom.audio.addEventListener("play", () => setPlayingState(true));
    dom.audio.addEventListener("pause", () => setPlayingState(false));
    dom.audio.addEventListener("ended", () => playNext(true));
    dom.audio.addEventListener("error", () => {
        if (!state.currentTrack) return;
        setPlayingState(false);
        showToast("Preview unavailable", "The browser could not play this source.", "error");
    });

    dom.themeToggle?.addEventListener("click", () => applyTheme(state.theme === "dark" ? "light" : "dark"));
    dom.refreshConnection?.addEventListener("click", checkConnection);
    document.getElementById("open-settings")?.addEventListener("click", openSettings);
    dom.settingsForm?.addEventListener("submit", saveSettings);
    dom.floatingDownload?.addEventListener("click", () => {
        const sidePanel = document.getElementById("side-panel");
        const sidebarBackdrop = document.getElementById("sidebar-backdrop");
        sidePanel?.classList.add("open");
        sidebarBackdrop?.classList.add("open");
        setActiveNav("downloads");
    });

    document.querySelectorAll(".quality-card").forEach((card) => {
        card.addEventListener("click", () => {
            const val = card.dataset.value;
            if (dom.qualitySetting) dom.qualitySetting.value = val;
            document.querySelectorAll(".quality-card").forEach((c) => c.classList.toggle("active", c === card));
        });
    });

    document.querySelectorAll(".engine-card").forEach((card) => {
        card.addEventListener("click", () => {
            const val = card.dataset.engine;
            const input = document.getElementById("engine-setting");
            if (input) input.value = val;
            document.querySelectorAll(".engine-card").forEach((c) => c.classList.toggle("active", c === card));
        });
    });

    const toggleSidePanel = document.getElementById("toggle-side-panel");
    const closeSidePanel = document.getElementById("close-side-panel");
    const sidePanel = document.getElementById("side-panel");
    const sidebarBackdrop = document.getElementById("sidebar-backdrop");

    function openSideDrawer() {
        sidePanel?.classList.add("open");
        sidebarBackdrop?.classList.add("open");
        toggleSidePanel?.setAttribute("aria-expanded", "true");
    }

    function closeSideDrawer() {
        sidePanel?.classList.remove("open");
        sidebarBackdrop?.classList.remove("open");
        toggleSidePanel?.setAttribute("aria-expanded", "false");
    }

    toggleSidePanel?.addEventListener("click", () => {
        if (sidePanel?.classList.contains("open")) closeSideDrawer();
        else openSideDrawer();
    });
    closeSidePanel?.addEventListener("click", closeSideDrawer);
    sidebarBackdrop?.addEventListener("click", closeSideDrawer);

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && sidePanel?.classList.contains("open")) {
            closeSideDrawer();
        }
    });

    document.addEventListener("keydown", handleKeyboard);
    document.querySelectorAll(".nav-item[data-section]").forEach((item) => {
        item.addEventListener("click", (event) => {
            event.preventDefault();
            const section = item.dataset.section;
            setActiveNav(section, true);
            closeSideDrawer();
            if (window.location.hash !== `#${section}`) {
                history.replaceState(null, "", `#${section}`);
            }
        });
    });
}

function runExampleSearch(query) {
    setSearchMode(/^https?:\/\//i.test(query) ? "link" : "music");
    dom.searchInput.value = query;
    dom.searchForm.requestSubmit();
}

function setSearchMode(mode) {
    state.searchMode = mode === "link" ? "link" : "music";
    document.querySelectorAll("[data-search-mode]").forEach((button) => button.classList.toggle("active", button.dataset.searchMode === state.searchMode));
    if (state.searchMode === "link") {
        dom.searchInput.placeholder = "Paste an Amazon, Tidal, Spotify, or YouTube link";
        if (dom.searchModeHint) dom.searchModeHint.textContent = "Amazon Music • Tidal • Spotify • YouTube links supported";
        if (dom.metadataFilters) dom.metadataFilters.hidden = true;
        dom.toggleFilters?.setAttribute("aria-expanded", "false");
    } else {
        dom.searchInput.placeholder = "Search a song, artist, album, or year";
        if (dom.searchModeHint) dom.searchModeHint.textContent = "Search by title, artist, album, or year.";
    }
    dom.searchInput.focus();
}

function clearMetadataFilters() {
    if (dom.artistFilter) dom.artistFilter.value = "";
    if (dom.albumFilter) dom.albumFilter.value = "";
    if (dom.yearFilter) dom.yearFilter.value = "";
    if (dom.durationFilter) dom.durationFilter.value = "any";
    if (dom.searchScope) dom.searchScope.value = "all";
    if (dom.availabilityFilter) dom.availabilityFilter.value = "all";
    applyFilters();
}

async function handleSearch(event) {
    event?.preventDefault();
    const primaryQuery = dom.searchInput.value.trim();
    const artist = dom.artistFilter?.value.trim() || "";
    const album = dom.albumFilter?.value.trim() || "";
    const year = dom.yearFilter?.value.trim() || "";
    if (!primaryQuery && !artist && !album && !year) {
        showToast("Add a search term", "Enter a title, artist, album, year, or supported link.", "error");
        dom.searchInput.focus();
        return;
    }
    if (state.searchMode === "link" && !/^https?:\/\//i.test(primaryQuery) && !primaryQuery.startsWith("tidal:") && !primaryQuery.startsWith("spotify:")) {
        showToast("That is not a link", "Paste a full Amazon Music, Tidal, Spotify, or YouTube URL.", "error");
        dom.searchInput.focus();
        return;
    }

    // Automatically navigate to Discover view if user searched from Downloads, Queue, etc.
    setActiveNav("discover", false);
    if (window.location.hash !== "#discover") {
        history.replaceState(null, "", "#discover");
    }

    state.searchController?.abort();
    state.searchController = new AbortController();
    setSearchLoading();

    try {
        let resolvedQuery = primaryQuery;
        let directAmazonTrack = null;
        let directTidalTrack = null;
        if (/^https?:\/\//i.test(primaryQuery) || primaryQuery.startsWith("tidal:") || primaryQuery.startsWith("spotify:")) {
            const resolved = await resolveLink(primaryQuery, state.searchController.signal);
            resolvedQuery = resolved.query;
            if (resolved.directSource === "amazon" && resolved.track) {
                directAmazonTrack = resolved.track;
                directAmazonTrack.inputUrl = primaryQuery;
            } else if (resolved.directSource === "tidal" && resolved.track) {
                directTidalTrack = resolved.track;
                directTidalTrack.inputUrl = primaryQuery;
            }
            if (state.searchMode !== "link" && resolvedQuery && resolvedQuery !== primaryQuery) {
                dom.searchInput.value = resolvedQuery;
            }
        }

        const catalogQuery = [resolvedQuery, artist, album, year].filter(Boolean).join(" ").trim();
        const [amazonResult, tidalResult, spotifyResult, previewResult] = await Promise.allSettled([
            directAmazonTrack ? Promise.resolve([directAmazonTrack]) : (directTidalTrack ? Promise.resolve([]) : searchAmazon(catalogQuery, state.searchController.signal)),
            directTidalTrack ? Promise.resolve([directTidalTrack]) : (directAmazonTrack ? Promise.resolve([]) : searchTidal(catalogQuery, state.searchController.signal)),
            searchSpotify(catalogQuery, state.searchController.signal),
            searchPreviews(catalogQuery, state.searchController.signal),
        ]);

        if (state.searchController.signal.aborted) return;
        const amazon = amazonResult.status === "fulfilled" && Array.isArray(amazonResult.value) ? amazonResult.value : [];
        const tidal = tidalResult.status === "fulfilled" && Array.isArray(tidalResult.value) ? tidalResult.value : [];
        const spotify = spotifyResult.status === "fulfilled" && Array.isArray(spotifyResult.value) ? spotifyResult.value : [];
        const initialPreviews = previewResult.status === "fulfilled" && Array.isArray(previewResult.value) ? previewResult.value : [];
        
        const allCatalogItems = [...amazon, ...tidal];
        const previews = allCatalogItems.length
            ? await findCatalogPreviewCandidates(allCatalogItems, spotify, initialPreviews, state.searchController.signal)
            : [];
        if (state.searchController.signal.aborted) return;
        state.results = mergeSearchSources(amazon, tidal, spotify, previews);

        if (!state.results.length) {
            const reasons = [amazonResult, tidalResult, previewResult]
                .filter((result) => result.status === "rejected")
                .map((result) => result.reason?.message)
                .filter(Boolean);
            renderSearchError(reasons[0] || "No tracks matched those details across Amazon & Tidal.", false);
            return;
        }

        addRecentSearch(catalogQuery);
        applyFilters();
        document.querySelector(".results-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
        if (error.name === "AbortError") return;
        renderSearchError(error.message || "Search could not be completed.", true);
    }
}

async function resolveLink(value, signal) {
    // 1. Amazon Music URL
    if (/music\.amazon\./i.test(value) || /amazon\.[^/]+\/music/i.test(value)) {
        const asinMatch = value.match(/(?:trackAsin=|tracks\/|albums\/)([A-Z0-9]{10})/i);
        if (asinMatch) {
            const track = await requestJson(api("/api/resolve"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ input: asinMatch[1], quality: state.quality }),
                signal,
            });
            return { query: `${track.title} ${track.artist}`, track, directSource: "amazon" };
        }
    }

    // 2. Tidal URL or URI
    if (/tidal\.com\//i.test(value) || value.startsWith("tidal:")) {
        const trackIdMatch = value.match(/(?:track\/|trackId=|tidal:track:)(\d+)/i);
        if (trackIdMatch) {
            const track = await requestJson(api("/api/tidal/resolve"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ input: trackIdMatch[1], quality: state.quality }),
                signal,
            });
            return { query: `${track.title} ${track.artist}`, track, directSource: "tidal" };
        }
    }

    // 3. Fallback resolve endpoint
    const data = await requestJson(api(`/api/resolve?q=${encodeURIComponent(value)}`), { signal });
    return { query: data.resolved || value, track: null, directSource: null };
}

async function searchAmazon(query, signal) {
    return requestJson(api(`/api/search?q=${encodeURIComponent(query)}&limit=20`), { signal });
}

async function searchTidal(query, signal) {
    return requestJson(api(`/api/tidal/search?q=${encodeURIComponent(query)}&limit=20`), { signal });
}

async function searchSpotify(query, signal) {
    return requestJson(api(`/api/spotify/search?q=${encodeURIComponent(query)}&limit=20`), { signal });
}

async function searchPreviews(query, signal) {
    try {
        const data = await requestJson(`${PREVIEW_API}/search/songs?query=${encodeURIComponent(query)}`, { signal });
        return data?.success && Array.isArray(data?.data?.results) ? data.data.results : [];
    } catch (e) {
        console.warn("Preview search notice:", e);
        return [];
    }
}

async function findCatalogPreviewCandidates(catalogItems, spotifyItems, initialCandidates, signal) {
    const unmatchedQueries = catalogItems
        .filter((item) => !findCatalogPreview(item, spotifyItems, initialCandidates))
        .map((item) => [item.title, item.artist].filter(Boolean).join(" ").trim())
        .filter(Boolean);
    const uniqueQueries = [...new Map(unmatchedQueries.map((query) => [normalize(query), query])).values()].slice(0, 16);
    if (!uniqueQueries.length) return initialCandidates;

    const searches = await Promise.allSettled(uniqueQueries.map((query) => searchPreviews(query, signal)));
    if (signal.aborted) return [];

    const candidates = [...initialCandidates];
    searches.forEach((result) => {
        if (result.status === "fulfilled") candidates.push(...result.value);
    });

    return [...new Map(candidates.map((item) => [item.id || getPreviewUrl(item) || item.url, item])).values()];
}

function cleanTitle(title) {
    if (!title) return "";
    return String(title)
        .replace(/\((?:official\s+)?(?:music\s+)?video\)/gi, "")
        .replace(/\[(?:official\s+)?(?:music\s+)?video\]/gi, "")
        .replace(/\((?:official\s+)?audio\)/gi, "")
        .replace(/\[(?:official\s+)?audio\]/gi, "")
        .replace(/\(lyric(?:s)?(?:\s+video)?\)/gi, "")
        .replace(/\[lyric(?:s)?(?:\s+video)?\]/gi, "")
        .replace(/\[(?:hd|hq|4k|1080p)\]/gi, "")
        .replace(/\((?:visualizer|clip\s+officiel)\)/gi, "")
        .replace(/\((?:explicit|clean)\)/gi, "")
        .replace(/\s*(?:feat\.|ft\.|featuring|with)\s+[^\(\)\[\]]+/gi, "")
        .replace(/[\(\[\{]\s*(?:feat\.|ft\.|featuring|with)[^\)\]\}]+[\)\]\}]/gi, "")
        .trim();
}

function cleanArtistTokens(artistStr) {
    if (!artistStr) return [];
    const norm = normalize(artistStr);
    if (!norm || norm.includes("unknown")) return [];
    return norm
        .split(/(?:,|\s+(?:feat\.|ft\.|featuring|with|and|&|x|\/|\+)\s+)/i)
        .map((s) => s.trim())
        .filter((s) => s.length > 1 && !["the", "and", "feat", "ft", "various"].includes(s));
}

function tokenSimilarity(left, right) {
    const a = new Set(normalize(left).split(" ").filter((token) => token.length > 1));
    const b = new Set(normalize(right).split(" ").filter((token) => token.length > 1));
    if (!a.size || !b.size) return 0;
    const overlap = [...a].filter((token) => b.has(token)).length;
    return overlap / Math.max(a.size, b.size);
}

function artistsCompatible(left, right) {
    const a = normalize(left);
    const b = normalize(right);
    if (!a || !b || a.includes("unknown") || b.includes("unknown")) return false;
    if (a === b || a.includes(b) || b.includes(a)) return true;
    const tokensA = cleanArtistTokens(left);
    const tokensB = cleanArtistTokens(right);
    if (!tokensA.length || !tokensB.length) return false;
    const overlap = tokensA.some((token) => tokensB.some((bToken) => bToken.includes(token) || token.includes(bToken)));
    return overlap || tokenSimilarity(a, b) >= 0.45;
}

function matchScore(base, candidate, candidateArtist = "") {
    const baseRaw = normalize(base.title);
    const candRaw = normalize(candidate.title || candidate.name);
    const baseClean = normalize(cleanTitle(base.title));
    const candClean = normalize(cleanTitle(candidate.title || candidate.name));
    
    let score = 0;
    if (baseRaw === candRaw || baseClean === candClean) {
        score += 15;
    } else {
        const simClean = tokenSimilarity(baseClean, candClean);
        const simRaw = tokenSimilarity(baseRaw, candRaw);
        const maxSim = Math.max(simClean, simRaw);
        if (maxSim >= 0.85) score += 12;
        else if (maxSim >= 0.70) score += 9;
        else if (maxSim >= 0.50) score += 6;
        else if ((baseClean.includes(candClean) || candClean.includes(baseClean)) && Math.min(baseClean.length, candClean.length) >= 5) score += 7;
    }

    const artistA = base.artist;
    const artistB = candidate.artist || candidateArtist;
    if (artistsCompatible(artistA, artistB)) {
        score += 8;
        if (normalize(artistA) === normalize(artistB)) score += 3;
    }

    if (base.album && candidate.album) {
        const albumA = normalize(base.album);
        const albumB = normalize(candidate.album?.name || candidate.album);
        if (albumA === albumB) score += 4;
        else if (tokenSimilarity(albumA, albumB) >= 0.6) score += 2;
    }

    const candidateDuration = Number(candidate.duration || candidate.duration_sec || 0);
    const baseDuration = Number(base.duration || base.duration_sec || 0);
    if (candidateDuration && baseDuration) {
        const difference = Math.abs(candidateDuration - baseDuration);
        if (difference <= 3) score += 4;
        else if (difference <= 8) score += 2;
        else if (difference > 25) score -= 6;
    }

    return score;
}

function bestMatch(base, candidates, artistGetter, options = {}) {
    const { minScore = 11, requireArtist = true, durationTolerance = 0 } = options;
    let winner = null;
    let winnerScore = -Infinity;
    candidates.forEach((candidate) => {
        const candidateArtist = artistGetter(candidate);
        if (requireArtist && !artistsCompatible(base.artist, candidateArtist)) return;
        const candidateDuration = Number(candidate.duration || candidate.duration_sec || 0);
        const baseDuration = Number(base.duration || base.duration_sec || 0);
        if (durationTolerance && candidateDuration && baseDuration && Math.abs(candidateDuration - baseDuration) > durationTolerance) return;
        const score = matchScore(base, candidate, candidateArtist);
        if (score > winnerScore) {
            winner = candidate;
            winnerScore = score;
        }
    });
    return winnerScore >= minScore ? winner : null;
}

function previewArtists(item) {
    return item?.artists?.primary?.map((artist) => artist.name).join(", ") || "Unknown artist";
}

const VERSION_MARKERS = ["acoustic", "ambient", "cover", "extended", "instrumental", "karaoke", "live", "nightcore", "radio edit", "remix", "slowed", "sped up", "techno"];

function hasVersionMarker(title, marker) {
    return ` ${normalize(title)} `.includes(` ${marker} `);
}

function versionsCompatible(catalogTitle, previewTitle) {
    return VERSION_MARKERS.every((marker) => hasVersionMarker(catalogTitle, marker) === hasVersionMarker(previewTitle, marker));
}

function isExactSpotifyMatch(base, spotify) {
    if (!spotify || !base) return false;
    const baseRaw = normalize(base.title || "");
    const candRaw = normalize(spotify.title || "");
    const baseClean = normalize(cleanTitle(base.title || ""));
    const candClean = normalize(cleanTitle(spotify.title || ""));
    const simClean = tokenSimilarity(baseClean, candClean);
    const simRaw = tokenSimilarity(baseRaw, candRaw);
    
    // Strict title match (must have high token similarity or identical clean title)
    const titleMatch = (baseClean && baseClean === candClean) || (baseRaw && baseRaw === candRaw) || simClean >= 0.85 || simRaw >= 0.85;
    if (!titleMatch) return false;

    // Strict artist match
    const baseArtist = base.artist;
    const candArtist = spotify.artist;
    if (baseArtist && !artistsCompatible(baseArtist, candArtist)) return false;

    // Strict duration match (within 8 seconds)
    const baseDur = Number(base.duration || base.duration_sec || 0);
    const candDur = Number(spotify.duration || spotify.duration_sec || 0);
    if (baseDur && candDur && Math.abs(baseDur - candDur) > 8) return false;

    return true;
}

function findCatalogPreview(item, spotifyItems, previewItems) {
    const rawArtist = item.artist && !normalize(item.artist).includes("unknown") ? item.artist : "";
    const rawSpotify = bestMatch(
        { title: item.title, artist: rawArtist, album: item.album || "", duration: Number(item.duration_sec || 0) },
        spotifyItems,
        (entry) => entry.artist || "",
        { minScore: 16, requireArtist: Boolean(rawArtist) }
    );
    const spotify = isExactSpotifyMatch(item, rawSpotify) ? rawSpotify : null;

    const artist = rawArtist || spotify?.artist || "Unknown artist";
    const base = {
        title: item.title || "Unknown title",
        artist,
        album: item.album || spotify?.album || "",
        duration: Number(item.duration_sec || 0),
    };
    const playableCandidates = previewItems.filter((candidate) => getPreviewUrl(candidate) && versionsCompatible(base.title, candidate.name));
    return bestMatch(base, playableCandidates, previewArtists, {
        minScore: 15,
        requireArtist: !normalize(artist).includes("unknown"),
        durationTolerance: 15,
    });
}

function mergeSearchSources(amazonItems, tidalItems, spotifyItems, previewItems) {
    const matchedTidalAsins = new Set();
    const matchedAmazonAsins = new Set();
    const results = [];

    // 1. Process Amazon items as base, matching corresponding Tidal and Spotify items
    amazonItems.forEach((amzItem, amzIdx) => {
        const rawArtist = amzItem.artist && !normalize(amzItem.artist).includes("unknown") ? amzItem.artist : "";

        // Find matching Spotify metadata (strictly verified)
        const rawSpotify = bestMatch(
            { title: amzItem.title, artist: rawArtist, album: amzItem.album || "", duration: Number(amzItem.duration_sec || 0) },
            spotifyItems,
            (entry) => entry.artist || "",
            { minScore: 16, requireArtist: Boolean(rawArtist) }
        );
        const spotify = isExactSpotifyMatch(amzItem, rawSpotify) ? rawSpotify : null;

        // Find matching Tidal track
        const matchedTidal = bestMatch(
            { title: amzItem.title, artist: rawArtist || spotify?.artist || "", album: amzItem.album || spotify?.album || "", duration: Number(amzItem.duration_sec || 0) },
            tidalItems.filter((t) => !matchedTidalAsins.has(String(t.asin))),
            (entry) => entry.artist || "",
            { minScore: 12, requireArtist: Boolean(rawArtist) }
        );

        if (matchedTidal) {
            matchedTidalAsins.add(String(matchedTidal.asin));
        }
        matchedAmazonAsins.add(String(amzItem.asin));

        // Audio preview resolution: 1. JioSaavn full stream -> 2. Spotify/Studio 30s preview
        const preview = findCatalogPreview(amzItem, spotify ? [spotify] : [], previewItems);
        const jioUrl = getPreviewUrl(preview);
        const spotify30s = spotify?.preview_url;
        const streamUrl = jioUrl || spotify30s || "";
        const streamType = jioUrl ? "full" : (spotify30s ? "spotify-30s" : "none");

        // Canonical native metadata preservation
        const title = amzItem.title || spotify?.title || "Unknown title";
        const artist = rawArtist || amzItem.artist || spotify?.artist || previewArtists(preview);
        const album = amzItem.album || spotify?.album || preview?.album?.name || "";
        const image = (amzItem.thumbnail_url ? upgradeThumbnail(amzItem.thumbnail_url) : "")
            || (matchedTidal?.thumbnail_url ? upgradeThumbnail(matchedTidal.thumbnail_url) : "")
            || (spotify?.thumbnail_hq ? upgradeThumbnail(spotify.thumbnail_hq) : "")
            || getImage(preview, "500x500")
            || spotify?.thumbnail_url
            || getImage(preview);

        results.push({
            id: `song-${amzItem.asin || amzIdx}`,
            asin: amzItem.asin || "",
            amazonAsin: amzItem.asin || "",
            tidalAsin: matchedTidal?.asin || "",
            title: title,
            artist: artist,
            album: album,
            year: amzItem.year || spotify?.year || matchedTidal?.year || preview?.year || "",
            releaseDate: amzItem.release_date || spotify?.release_date || matchedTidal?.release_date || "",
            duration: Number(amzItem.duration_sec || spotify?.duration_sec || matchedTidal?.duration_sec || preview?.duration || 0),
            image: image,
            amazonUrl: safeUrl(amzItem.url || amzItem.inputUrl || ""),
            tidalUrl: matchedTidal ? safeUrl(matchedTidal.url || `https://tidal.com/browse/track/${matchedTidal.asin}`) : "",
            spotifyUrl: spotify?.spotify_id ? `https://open.spotify.com/track/${spotify.spotify_id}` : "",
            previewPageUrl: safeUrl(preview?.url || ""),
            streamUrl: streamUrl,
            streamType: streamType,
            previewId: preview?.id || "",
            spotifyId: spotify?.spotify_id || "",
            codec: "flac",
            bitrate: amzItem.bitrate || 0,
            language: preview?.language || "",
            label: preview?.label || "",
            copyright: preview?.copyright || "",
            playCount: preview?.playCount || "",
            genre: amzItem.genre || matchedTidal?.genre || "",
            trackNumber: amzItem.track_number || matchedTidal?.track_number || "",
            discNumber: amzItem.disc_number || matchedTidal?.disc_number || "",
            explicit: Boolean(amzItem.explicit || matchedTidal?.explicit || preview?.explicitContent),
            source: "spotify_unified",
            downloadSource: (matchedTidal && state.enginePriority === "tidal") ? "tidal" : "amazon",
            downloadInput: (matchedTidal && state.enginePriority === "tidal") ? (matchedTidal.asin || matchedTidal.url) : (amzItem.asin || amzItem.url || amzItem.title),
            hasAmazon: true,
            hasTidal: Boolean(matchedTidal),
            hasBothSources: Boolean(matchedTidal),
            audioQuality: state.quality || "UHD",
            relevance: amzIdx,
        });
    });

    // 2. Process Tidal-only items (Tracks present in Tidal only)
    tidalItems.forEach((tItem, tIdx) => {
        if (matchedTidalAsins.has(String(tItem.asin))) return;

        const rawArtist = tItem.artist && !normalize(tItem.artist).includes("unknown") ? tItem.artist : "";

        // Find matching Spotify metadata (strictly verified)
        const rawSpotify = bestMatch(
            { title: tItem.title, artist: rawArtist, album: tItem.album || "", duration: Number(tItem.duration_sec || 0) },
            spotifyItems,
            (entry) => entry.artist || "",
            { minScore: 16, requireArtist: Boolean(rawArtist) }
        );
        const spotify = isExactSpotifyMatch(tItem, rawSpotify) ? rawSpotify : null;

        // Audio preview resolution: 1. JioSaavn full stream -> 2. Spotify/Studio 30s preview
        const preview = findCatalogPreview(tItem, spotify ? [spotify] : [], previewItems);
        const jioUrl = getPreviewUrl(preview);
        const spotify30s = spotify?.preview_url;
        const streamUrl = jioUrl || spotify30s || "";
        const streamType = jioUrl ? "full" : (spotify30s ? "spotify-30s" : "none");

        // Canonical native metadata preservation
        const title = tItem.title || spotify?.title || "Unknown title";
        const artist = rawArtist || tItem.artist || spotify?.artist || previewArtists(preview);
        const album = tItem.album || spotify?.album || preview?.album?.name || "";
        const image = (tItem.thumbnail_url ? upgradeThumbnail(tItem.thumbnail_url) : "")
            || (spotify?.thumbnail_hq ? upgradeThumbnail(spotify.thumbnail_hq) : "")
            || getImage(preview, "500x500")
            || spotify?.thumbnail_url
            || getImage(preview);

        results.push({
            id: `song-tidal-${tItem.asin || tIdx}`,
            asin: tItem.asin || "",
            amazonAsin: "",
            tidalAsin: tItem.asin || "",
            title: title,
            artist: artist,
            album: album,
            year: tItem.year || spotify?.year || preview?.year || "",
            releaseDate: tItem.release_date || spotify?.release_date || "",
            duration: Number(tItem.duration_sec || spotify?.duration_sec || preview?.duration || 0),
            image: image,
            amazonUrl: "",
            tidalUrl: safeUrl(tItem.url || tItem.inputUrl || `https://tidal.com/browse/track/${tItem.asin}`),
            spotifyUrl: spotify?.spotify_id ? `https://open.spotify.com/track/${spotify.spotify_id}` : "",
            previewPageUrl: safeUrl(preview?.url || ""),
            streamUrl: streamUrl,
            streamType: streamType,
            previewId: preview?.id || "",
            spotifyId: spotify?.spotify_id || "",
            codec: "flac",
            bitrate: 0,
            language: preview?.language || "",
            label: preview?.label || "",
            copyright: preview?.copyright || "",
            playCount: preview?.playCount || "",
            genre: tItem.genre || "",
            trackNumber: tItem.track_number || "",
            discNumber: tItem.disc_number || "",
            explicit: Boolean(tItem.explicit || preview?.explicitContent),
            source: "spotify_unified",
            downloadSource: "tidal",
            downloadInput: tItem.asin || tItem.url || tItem.title,
            hasAmazon: false,
            hasTidal: true,
            hasBothSources: false,
            audioQuality: tItem.audio_quality || "HI_RES",
            relevance: amazonItems.length + tIdx,
        });
    });

    return results;
}

function setSearchLoading() {
    dom.resultsTitle.textContent = "Searching catalog & metadata…";
    dom.resultsSummary.textContent = "Matching Spotify metadata with Amazon & Tidal lossless sources.";
    dom.searchResults.innerHTML = `<div class="loading-list">${Array.from({ length: 6 }, () => '<div class="skeleton-row"></div>').join("")}</div>`;
}

function applyFilters() {
    const artist = dom.artistFilter ? normalize(dom.artistFilter.value) : "";
    const album = dom.albumFilter ? normalize(dom.albumFilter.value) : "";
    const year = dom.yearFilter ? dom.yearFilter.value.trim() : "";
    const duration = dom.durationFilter ? dom.durationFilter.value : "any";
    const scope = dom.searchScope ? dom.searchScope.value : "all";
    const availability = dom.availabilityFilter ? dom.availabilityFilter.value : "all";
    const scopedQuery = state.searchMode === "music" ? normalize(dom.searchInput?.value || "") : "";

    state.filteredResults = state.results.filter((track) => {
        if (scope !== "all" && scopedQuery && !normalize(track[scope]).includes(scopedQuery)) return false;
        if (artist && !normalize(track.artist).includes(artist)) return false;
        if (album && !normalize(track.album).includes(album)) return false;
        if (year && String(track.year) !== year) return false;
        if (duration === "short" && track.duration >= 180) return false;
        if (duration === "medium" && (track.duration < 180 || track.duration > 300)) return false;
        if (duration === "long" && track.duration <= 300) return false;
        if (availability === "playable" && !track.streamUrl) return false;
        if (availability === "lossless" && !track.hasAmazon && !track.hasTidal) return false;
        return true;
    });

    const sort = dom.sortResults ? dom.sortResults.value : "relevance";
    state.filteredResults.sort((a, b) => {
        if (sort === "title") return a.title.localeCompare(b.title);
        if (sort === "artist") return a.artist.localeCompare(b.artist);
        if (sort === "duration") return (a.duration || Infinity) - (b.duration || Infinity);
        if (sort === "year") return Number(b.year || 0) - Number(a.year || 0);
        return a.relevance - b.relevance;
    });
    renderResults();
}

function renderResults() {
    const count = state.filteredResults.length;
    const playableCount = state.filteredResults.filter((track) => track.streamUrl).length;
    const bothCount = state.filteredResults.filter((track) => track.hasBothSources).length;
    const amzOnlyCount = state.filteredResults.filter((track) => track.hasAmazon && !track.hasTidal).length;
    const tidalOnlyCount = state.filteredResults.filter((track) => track.hasTidal && !track.hasAmazon).length;
    
    let summaryParts = [`${playableCount} playable`];
    if (bothCount) summaryParts.push(`${bothCount} Amazon & Tidal`);
    if (amzOnlyCount) summaryParts.push(`${amzOnlyCount} Amazon only`);
    if (tidalOnlyCount) summaryParts.push(`${tidalOnlyCount} Tidal only`);
    summaryParts.push("Spotify metadata");

    dom.resultsTitle.textContent = count ? `${count} unified track${count === 1 ? "" : "s"}` : "No matching tracks";
    dom.resultsSummary.textContent = count ? summaryParts.join(" · ") : "Try broadening the metadata filters.";

    if (!count) {
        dom.searchResults.innerHTML = `<div class="empty-state"><div class="empty-main"><div class="empty-icon">${icon("filter")}</div><div class="empty-text"><h3>No filtered results</h3><p>Clear or broaden the artist, album, year, or duration criteria.</p></div></div></div>`;
        return;
    }

    dom.searchResults.innerHTML = state.filteredResults.map((track) => {
        const selected = state.selectedTrack?.id === track.id ? " selected" : "";
        const playing = state.currentTrack?.id === track.id ? " now-playing" : "";
        const playable = Boolean(track.streamUrl);
        const unavailable = playable ? "" : " preview-unavailable";

        let sourceBadge = '<span class="badge">Direct audio</span>';
        if (track.hasBothSources) {
            sourceBadge = '<span class="badge lossless" title="Available in Ultra HD on Amazon & Tidal">Amazon & Tidal FLAC</span>';
        } else if (track.hasAmazon) {
            sourceBadge = '<span class="badge lossless" title="Available in Ultra HD on Amazon Music">Amazon FLAC</span>';
        } else if (track.hasTidal) {
            sourceBadge = '<span class="badge lossless tidal-badge" title="Available in Hi-Res on Tidal">Tidal FLAC</span>';
        }

        let previewBadge = '<span class="badge unavailable">No preview</span>';
        if (track.streamType === "full") {
            previewBadge = '<span class="badge preview">Hi-Fi Stream</span>';
        } else if (track.streamType === "spotify-30s") {
            previewBadge = '<span class="badge preview">30s Preview</span>';
        }

        let catalogLabel = track.hasBothSources
            ? "Spotify metadata · Amazon + Tidal FLAC"
            : (track.hasAmazon ? "Spotify metadata · Amazon FLAC" : (track.hasTidal ? "Spotify metadata · Tidal FLAC" : "Spotify metadata"));

        return `<article class="result-card${selected}${playing}${unavailable}" data-track-id="${escapeHtml(track.id)}" tabindex="0">
            <div class="result-art">
                ${imageMarkup(track.image, `${track.title} cover`)}
                ${playable ? `<button class="art-play" type="button" data-action="play" aria-label="Play ${escapeHtml(track.title)}">${icon(dom.audio && !dom.audio.paused && playing ? "pause" : "play")}</button>` : ""}
            </div>
            <div class="result-main">
                <strong title="${escapeHtml(track.title)}">${escapeHtml(track.title)}</strong>
                <span>${escapeHtml(track.artist)}</span>
                <div class="result-badges">
                    ${sourceBadge}
                    ${previewBadge}
                    ${track.year ? `<span class="badge">${escapeHtml(track.year)}</span>` : ""}
                    ${track.language ? `<span class="badge">${escapeHtml(track.language)}</span>` : ""}
                </div>
            </div>
            <div class="result-album"><strong>${escapeHtml(track.album || "Single / unknown album")}</strong><span>${escapeHtml(catalogLabel)}</span></div>
            <span class="result-duration">${formatDuration(track.duration)}</span>
            <div class="result-actions">
                ${playable ? `<button class="action-button" type="button" data-action="queue" aria-label="Add to play queue">${icon("plus")}</button>` : ""}
                <button class="action-button primary" type="button" data-action="download" aria-label="Download ${escapeHtml(track.title)}" ${!track.downloadInput ? "disabled" : ""}>${icon("download")}</button>
            </div>
        </article>`;
    }).join("");

    dom.searchResults.querySelectorAll("img").forEach((image) => image.addEventListener("error", () => image.remove(), { once: true }));
}

function renderSearchError(message, isError) {
    state.results = [];
    state.filteredResults = [];
    dom.resultsTitle.textContent = isError ? "Search unavailable" : "No matches found";
    dom.resultsSummary.textContent = isError ? "The catalog could not complete this request." : "Try a simpler title or artist search.";
    dom.searchResults.innerHTML = `<div class="empty-state ${isError ? "error-state" : ""}"><div class="empty-main"><div class="empty-icon">${icon(isError ? "alert" : "search")}</div><div class="empty-text"><h3>${isError ? "We could not finish that search" : "Nothing matched"}</h3><p>${escapeHtml(message)}</p></div></div><button class="secondary-button" type="button" data-retry-search>Try again</button></div>`;
    dom.searchResults.querySelector("[data-retry-search]")?.addEventListener("click", () => dom.searchForm.requestSubmit());
}

function handleResultAction(event) {
    const card = event.target.closest("[data-track-id]");
    if (!card) return;
    const track = state.filteredResults.find((item) => item.id === card.dataset.trackId);
    if (!track) return;
    const action = event.target.closest("[data-action]")?.dataset.action;

    if (action === "play") {
        if (state.currentTrack?.id === track.id && !dom.audio.paused) togglePlayback();
        else playTrack(track, { setContext: true });
    } else if (action === "queue") {
        addToQueue(track);
    } else if (action === "download") {
        startDownload(track);
    } else {
        selectTrack(track);
    }
}

function selectTrack(track) {
    state.selectedTrack = track;
    renderInspector();
    renderResults();
}

function renderInspector() {
    const track = state.selectedTrack;
    if (!dom.inspector || !track) return;
    const playable = Boolean(track.streamUrl);

    let losslessBadge = `<span class="badge">${escapeHtml(track.codec || "AAC")}</span>`;
    if (track.hasBothSources) {
        losslessBadge = '<span class="badge lossless">Amazon & Tidal FLAC</span>';
    } else if (track.hasAmazon) {
        losslessBadge = '<span class="badge lossless">Amazon 24-bit FLAC</span>';
    } else if (track.hasTidal) {
        losslessBadge = '<span class="badge lossless tidal-badge">Tidal Hi-Res FLAC</span>';
    }

    let sourceFact = "Spotify Metadata";
    if (track.hasBothSources) sourceFact = "Amazon Music (UHD) + Tidal (HiFi)";
    else if (track.hasAmazon) sourceFact = "Amazon Music (Ultra HD)";
    else if (track.hasTidal) sourceFact = "Tidal Music (Hi-Res)";

    let previewFact = "No Browser Preview";
    if (track.streamType === "full") previewFact = "Hi-Fi Full Audio (320 kbps)";
    else if (track.streamType === "spotify-30s") previewFact = "Spotify 30-Sec Preview";

    const facts = [
        ["Lossless FLAC", sourceFact],
        ["Audio Stream", previewFact],
        ["Released", track.releaseDate || track.year],
        ["Duration", formatDuration(track.duration)],
        ["Genre", track.genre],
        ["Track", track.trackNumber ? `${track.trackNumber}${track.discNumber ? ` · Disc ${track.discNumber}` : ""}` : ""],
    ].filter(([, value]) => value && value !== "—" && value !== "Unknown");

    const tidalAltBtn = (track.hasBothSources && track.tidalAsin)
        ? `<button class="secondary-button" type="button" data-inspector-action="download-tidal" title="Download via Tidal">${icon("download")} Download Tidal FLAC</button>`
        : "";

    dom.inspector.className = "inspector-content";
    dom.inspector.innerHTML = `
        <div class="inspector-identity">
            <div class="inspector-cover">${imageMarkup(track.image, `${track.title} cover`)}</div>
            <div class="inspector-title"><h3>${escapeHtml(track.title)}</h3><p>${escapeHtml(track.artist)}</p><span>${escapeHtml(track.album || "Album unavailable")}</span></div>
        </div>
        <div class="result-badges">
            ${losslessBadge}
            ${playable ? `<span class="badge preview">${track.streamType === "full" ? "Hi-Fi Stream" : "30s Preview"}</span>` : '<span class="badge unavailable">Download only</span>'}
            ${track.explicit ? '<span class="badge unavailable">Explicit</span>' : ""}
        </div>
        ${facts.length ? `<div class="track-facts">${facts.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong title="${escapeHtml(value)}">${escapeHtml(value)}</strong></div>`).join("")}</div>` : ""}
        ${playable ? `<div class="inspector-actions"><button class="primary-button" type="button" data-inspector-action="play">${icon("play")} Play preview</button><button class="secondary-button" type="button" data-inspector-action="download" aria-label="Download">${icon("download")} Download FLAC</button>${tidalAltBtn}</div>` : `<div class="no-preview-note">${icon("alert")} No verified browser preview. Playback and queue controls are hidden.</div><div class="inspector-actions"><button class="primary-button" type="button" data-inspector-action="download">${icon("download")} Download FLAC</button>${tidalAltBtn}</div>`}`;
    dom.inspector.querySelector("img")?.addEventListener("error", (event) => event.target.remove(), { once: true });
}

function handleInspectorAction(event) {
    const action = event.target.closest("[data-inspector-action]")?.dataset.inspectorAction;
    if (!action || !state.selectedTrack) return;
    if (action === "play" && state.selectedTrack.streamUrl) playTrack(state.selectedTrack, { setContext: true });
    if (action === "download") startDownload(state.selectedTrack);
    if (action === "download-tidal") {
        const tidalTrackCopy = {
            ...state.selectedTrack,
            downloadSource: "tidal",
            downloadInput: state.selectedTrack.tidalAsin || state.selectedTrack.downloadInput
        };
        startDownload(tidalTrackCopy);
    }
}

async function resolvePreview(track) {
    if (!track.streamUrl) throw new Error("No verified browser preview is available for this track.");
    return track.streamUrl;
}

async function playTrack(track, options = {}) {
    const { setContext = false, fromHistory = false } = options;
    if (!track.streamUrl) {
        showToast("No preview available", "This track can be downloaded, but it cannot be played in the browser.", "error");
        return;
    }
    if (state.currentTrack && state.currentTrack.id !== track.id && !fromHistory) state.history.push(state.currentTrack);
    if (setContext) {
        state.context = state.filteredResults.filter((item) => item.streamUrl);
        state.contextIndex = state.context.findIndex((item) => item.id === track.id);
    } else {
        const position = state.context.findIndex((item) => item.id === track.id);
        if (position >= 0) state.contextIndex = position;
    }

    state.currentTrack = track;
    state.selectedTrack = track;
    updatePlayerTrack(track, true);
    renderInspector();
    renderQueue();
    renderResults();

    const controller = new AbortController();
    try {
        const streamUrl = await resolvePreview(track, controller.signal);
        if (state.currentTrack?.id !== track.id) return;
        if (dom.audio.src !== streamUrl) {
            dom.audio.src = streamUrl;
            dom.audio.load();
        }
        updatePlayerTrack(track, false);
        await dom.audio.play();
        setupMediaSession(track);
    } catch (error) {
        if (state.currentTrack?.id !== track.id) return;
        updatePlayerTrack(track, false);
        showToast("Preview unavailable", error.message, "error");
    }
}

function updatePlayerTrack(track, loading = false) {
    const title = loading ? "Loading preview…" : track.title;
    dom.playerTitle.textContent = title;
    dom.playerArtist.textContent = loading ? track.title : track.artist;
    dom.sheetTitle.textContent = title;
    dom.sheetArtist.textContent = loading ? track.title : track.artist;
    dom.sheetAlbum.textContent = track.album || "";
    const artwork = imageMarkup(track.image, `${track.title} cover`);
    dom.playerCover.innerHTML = artwork;
    dom.sheetCover.innerHTML = artwork;
    dom.downloadCurrent.disabled = !track.downloadInput;
    dom.sheetDownload.disabled = !track.downloadInput;
    dom.playerCover.querySelector("img")?.addEventListener("error", (event) => event.target.remove(), { once: true });
    dom.sheetCover.querySelector("img")?.addEventListener("error", (event) => event.target.remove(), { once: true });
}

function togglePlayback() {
    if (!state.currentTrack) {
        showToast("Nothing is loaded", "Choose a result to start a preview.", "error");
        return;
    }
    if (!dom.audio.src) {
        playTrack(state.currentTrack);
        return;
    }
    if (dom.audio.paused) dom.audio.play().catch((error) => showToast("Playback failed", error.message, "error"));
    else dom.audio.pause();
}

function setPlayingState(isPlaying) {
    const iconName = isPlaying ? "pause" : "play";
    dom.playButtonIcon.innerHTML = `<use href="#icon-${iconName}"></use>`;
    dom.sheetPlayIcon.innerHTML = `<use href="#icon-${iconName}"></use>`;
    dom.playButton.setAttribute("aria-label", isPlaying ? "Pause" : "Play");
    dom.sheetPlay.setAttribute("aria-label", isPlaying ? "Pause" : "Play");
    dom.playerCover.classList.toggle("playing", isPlaying);
    if ("mediaSession" in navigator) {
        navigator.mediaSession.playbackState = isPlaying ? "playing" : "paused";
    }
    renderResults();
}

function playNext(fromEnded) {
    if (!state.currentTrack) return;
    if (fromEnded && state.repeatMode === "one") {
        dom.audio.currentTime = 0;
        dom.audio.play();
        return;
    }

    if (state.queue.length) {
        const next = state.queue.shift();
        renderQueue();
        playTrack(next);
        return;
    }

    if (!state.context.length) return;
    let nextIndex;
    if (state.shuffle && state.context.length > 1) {
        do nextIndex = Math.floor(Math.random() * state.context.length);
        while (nextIndex === state.contextIndex);
    } else {
        nextIndex = state.contextIndex + 1;
    }

    if (nextIndex >= state.context.length) {
        if (state.repeatMode === "all") nextIndex = 0;
        else {
            dom.audio.pause();
            dom.audio.currentTime = 0;
            return;
        }
    }
    state.contextIndex = nextIndex;
    playTrack(state.context[nextIndex]);
}

function playPrevious() {
    if (dom.audio.currentTime > 5) {
        dom.audio.currentTime = 0;
        return;
    }
    const previous = state.history.pop();
    if (previous) playTrack(previous, { fromHistory: true });
    else if (state.contextIndex > 0) {
        state.contextIndex -= 1;
        playTrack(state.context[state.contextIndex], { fromHistory: true });
    } else {
        dom.audio.currentTime = 0;
    }
}

function addToQueue(track) {
    if (!track.streamUrl) {
        showToast("Cannot queue this track", "A verified preview is not available.", "error");
        return;
    }
    if (state.queue.some((item) => item.id === track.id)) {
        showToast("Already queued", `${track.title} is already in Up next.`);
        return;
    }
    state.queue.push(track);
    renderQueue();
    showToast("Added to queue", track.title);
}

function clearPlayQueue() {
    state.queue = [];
    state.context = state.currentTrack ? [state.currentTrack] : [];
    state.contextIndex = state.currentTrack ? 0 : -1;
    renderQueue();
}

function renderQueue() {
    const contextual = state.context.slice(state.contextIndex + 1, state.contextIndex + 5);
    const seen = new Set(state.queue.map((track) => track.id));
    const items = [...state.queue.map((track) => ({ track, explicit: true })), ...contextual.filter((track) => !seen.has(track.id)).map((track) => ({ track, explicit: false }))].slice(0, 8);
    dom.navQueueCount.textContent = String(items.length);

    if (!items.length) {
        dom.queueList.innerHTML = `<div class="mini-empty">${icon("queue")}<span>Your play queue is empty</span></div>`;
        return;
    }
    dom.queueList.innerHTML = items.map(({ track, explicit }, index) => `<div class="queue-item" data-queue-id="${escapeHtml(track.id)}" data-explicit="${explicit}">
        <div class="queue-art">${imageMarkup(track.image, "")}</div>
        <div class="queue-copy"><strong>${escapeHtml(track.title)}</strong><span>${index === 0 ? "Up next · " : ""}${escapeHtml(track.artist)}</span></div>
        ${explicit ? `<button class="queue-remove" type="button" data-remove-queue aria-label="Remove ${escapeHtml(track.title)}">${icon("x")}</button>` : ""}
    </div>`).join("");
}

function handleQueueAction(event) {
    const item = event.target.closest("[data-queue-id]");
    if (!item) return;
    const track = [...state.queue, ...state.context].find((entry) => entry.id === item.dataset.queueId);
    if (!track) return;
    if (event.target.closest("[data-remove-queue]")) {
        state.queue = state.queue.filter((entry) => entry.id !== track.id);
        renderQueue();
    } else {
        if (item.dataset.explicit === "true") state.queue = state.queue.filter((entry) => entry.id !== track.id);
        playTrack(track);
    }
}

function toggleShuffle() {
    state.shuffle = !state.shuffle;
    dom.shuffleButton.classList.toggle("active", state.shuffle);
    showToast("Shuffle", state.shuffle ? "Playback order is randomized." : "Playback follows result order.");
}

function cycleRepeat() {
    const order = ["off", "all", "one"];
    state.repeatMode = order[(order.indexOf(state.repeatMode) + 1) % order.length];
    dom.repeatButton.classList.toggle("active", state.repeatMode !== "off");
    dom.repeatButton.classList.toggle("repeat-one", state.repeatMode === "one");
    dom.repeatButton.setAttribute("aria-label", `Repeat ${state.repeatMode}`);
}

function syncTimeline() {
    const duration = Number.isFinite(dom.audio.duration) ? dom.audio.duration : 0;
    const current = Number.isFinite(dom.audio.currentTime) ? dom.audio.currentTime : 0;
    const value = duration ? Math.round((current / duration) * 1000) : 0;
    dom.seekSlider.value = String(value);
    dom.sheetSeek.value = String(value);
    updateRangeFill(dom.seekSlider, value / 10);
    updateRangeFill(dom.sheetSeek, value / 10);
    dom.currentTime.textContent = formatDuration(current).replace("—", "0:00");
    dom.sheetCurrentTime.textContent = dom.currentTime.textContent;
    dom.totalTime.textContent = formatDuration(duration).replace("—", formatDuration(state.currentTrack?.duration).replace("—", "0:00"));
    dom.sheetTotalTime.textContent = dom.totalTime.textContent;
    const miniFill = document.getElementById("player-mini-progress-fill");
    if (miniFill) miniFill.style.width = `${value / 10}%`;
}

function seekFrom(slider) {
    if (!Number.isFinite(dom.audio.duration)) return;
    dom.audio.currentTime = (Number(slider.value) / 1000) * dom.audio.duration;
    syncTimeline();
}

function handleVolume() {
    const value = Number(dom.volumeSlider.value);
    dom.audio.volume = value / 100;
    localStorage.setItem("clash-volume", String(value));
    updateRangeFill(dom.volumeSlider, value);
}

function updateRangeFill(input, percent) {
    input.style.setProperty("--range-value", `${Math.min(100, Math.max(0, percent))}%`);
}

function setupMediaSession(track) {
    if (!("mediaSession" in navigator) || !("MediaMetadata" in window)) return;
    const artworkUrl = safeUrl(track.image);
    navigator.mediaSession.metadata = new MediaMetadata({
        title: track.title,
        artist: track.artist,
        album: track.album,
        artwork: artworkUrl ? [{ src: artworkUrl, sizes: "512x512" }] : [],
    });
    navigator.mediaSession.setActionHandler("play", () => dom.audio.play());
    navigator.mediaSession.setActionHandler("pause", () => dom.audio.pause());
    navigator.mediaSession.setActionHandler("previoustrack", playPrevious);
    navigator.mediaSession.setActionHandler("nexttrack", () => playNext(false));
    try {
        navigator.mediaSession.setActionHandler("seekbackward", (details) => seekRelative(-(details.seekOffset || 10)));
        navigator.mediaSession.setActionHandler("seekforward", (details) => seekRelative(details.seekOffset || 10));
        navigator.mediaSession.setActionHandler("seekto", (details) => {
            if (details.seekTime !== undefined && Number.isFinite(details.seekTime)) {
                dom.audio.currentTime = details.seekTime;
                syncTimeline();
            }
        });
        navigator.mediaSession.setActionHandler("stop", () => {
            dom.audio.pause();
            dom.audio.currentTime = 0;
        });
    } catch {
        // Fallback for browsers that don't support extended media session actions
    }
}

async function startDownload(track) {
    if (!track.downloadInput) {
        showToast("Download unavailable", "This result has no downloadable source.", "error");
        return;
    }
    if (state.downloads.some((job) => job.track.id === track.id && ["preparing", "downloading"].includes(job.status))) {
        showToast("Already downloading", track.title);
        return;
    }

    const controller = new AbortController();
    const job = { id: crypto.randomUUID?.() || `download-${Date.now()}`, track, controller, status: "preparing", progress: 0, message: "Preparing source" };
    state.downloads.unshift(job);
    renderDownloads();
    setActiveNav("downloads");

    try {
        if (track.downloadSource === "amazon") {
            await downloadAmazon(job);
        } else if (track.downloadSource === "tidal") {
            await downloadTidal(job);
        } else {
            await downloadPreview(job);
        }
        job.status = "completed";
        job.progress = 100;
        job.message = (track.downloadSource === "amazon" || track.downloadSource === "tidal") ? "Lossless copy ready" : "Audio file ready";
        showToast("Download complete", track.title);
    } catch (error) {
        if (error.name === "AbortError") {
            job.status = "cancelled";
            job.message = "Cancelled";
        } else {
            job.status = "failed";
            job.message = error.message || "Download failed";
            showToast("Download failed", job.message, "error");
        }
    } finally {
        renderDownloads();
    }
}

async function downloadAmazon(job) {
    job.status = "downloading";
    job.message = "Resolving and tagging Amazon FLAC";
    renderDownloads();
    const token = await getTurnstileToken();
    const dlUrl = api("/api/download");
    const headers = { "Content-Type": "application/json" };
    if (token) headers["X-Turnstile-Token"] = token;

    const amazonTarget = (job.track.downloadSource === "amazon" ? (job.track.amazonAsin || job.track.asin) : null)
        || job.track.amazonAsin
        || (job.track.asin && /^[A-Z0-9]{10}$/i.test(job.track.asin) ? job.track.asin : null)
        || `${job.track.title} ${job.track.artist}`.trim();

    const response = await fetch(dlUrl, {
        method: "POST",
        headers,
        body: JSON.stringify({
            input: amazonTarget,
            track: job.track,
            quality: state.quality
        }),
        signal: job.controller.signal,
    });
    resetTurnstile();
    if (!response.ok) {
        let message = `Amazon download failed (${response.status})`;
        try { message = (await response.json()).detail || message; } catch { /* Use fallback. */ }
        throw new Error(message);
    }

    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("audio") || contentType.includes("application/octet-stream")) {
        const blob = await response.blob();
        const disposition = response.headers.get("content-disposition") || "";
        const encoded = disposition.match(/filename\*=utf-8''([^;]+)/i)?.[1];
        const rawPlain = disposition.match(/filename="?([^";]+)"?/i)?.[1];
        const plain = rawPlain ? decodeURIComponent(rawPlain) : null;
        const filename = encoded ? decodeURIComponent(encoded) : (plain || `${fileSafe(job.track.title)}.flac`);
        saveBlob(blob, filename);
    } else {
        let errorMsg = "Server did not return an audio stream.";
        try {
            const result = await response.json();
            errorMsg = result.detail || result.message || errorMsg;
        } catch { /* ignore */ }
        throw new Error(errorMsg);
    }
}

async function downloadTidal(job) {
    job.status = "downloading";
    job.message = "Resolving and downloading Tidal FLAC";
    renderDownloads();
    const token = await getTurnstileToken();
    const dlUrl = api("/api/tidal/download");
    const headers = { "Content-Type": "application/json" };
    if (token) headers["X-Turnstile-Token"] = token;

    const tidalTarget = job.track.tidalAsin
        || (job.track.downloadSource === "tidal" ? (job.track.asin || job.track.id) : null)
        || (job.track.asin && /^\d+$/.test(job.track.asin) ? job.track.asin : null)
        || `${job.track.title} ${job.track.artist}`.trim();

    const response = await fetch(dlUrl, {
        method: "POST",
        headers,
        body: JSON.stringify({
            input: tidalTarget,
            track: job.track,
            quality: state.quality
        }),
        signal: job.controller.signal,
    });
    resetTurnstile();
    if (!response.ok) {
        let message = `Tidal download failed (${response.status})`;
        try { message = (await response.json()).detail || message; } catch { /* Use fallback. */ }
        throw new Error(message);
    }

    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("audio") || contentType.includes("application/octet-stream")) {
        const blob = await response.blob();
        const disposition = response.headers.get("content-disposition") || "";
        const encoded = disposition.match(/filename\*=utf-8''([^;]+)/i)?.[1];
        const rawPlain = disposition.match(/filename="?([^";]+)"?/i)?.[1];
        const plain = rawPlain ? decodeURIComponent(rawPlain) : null;
        const filename = encoded ? decodeURIComponent(encoded) : (plain || `${fileSafe(job.track.title)}.flac`);
        saveBlob(blob, filename);
    } else {
        let errorMsg = "Server did not return a Tidal audio stream.";
        try {
            const result = await response.json();
            errorMsg = result.detail || result.message || errorMsg;
        } catch { /* ignore */ }
        throw new Error(errorMsg);
    }
}

async function downloadPreview(job) {
    job.status = "downloading";
    job.message = "Downloading preview source";
    renderDownloads();
    const streamUrl = await resolvePreview(job.track, job.controller.signal);
    const response = await fetch(streamUrl, { signal: job.controller.signal });
    if (!response.ok) throw new Error(`Audio source returned ${response.status}`);
    const total = Number(response.headers.get("content-length") || 0);
    if (!response.body) {
        saveBlob(await response.blob(), `${fileSafe(job.track.title)}.m4a`);
        return;
    }

    const reader = response.body.getReader();
    const chunks = [];
    let received = 0;
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        received += value.length;
        if (total) {
            job.progress = Math.min(99, Math.round((received / total) * 100));
            job.message = `${job.progress}% downloaded`;
            renderDownloads();
        }
    }
    saveBlob(new Blob(chunks, { type: response.headers.get("content-type") || "audio/mp4" }), `${fileSafe(job.track.title)}.m4a`);
}

function saveBlob(blob, filename) {
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    setTimeout(() => {
        anchor.remove();
        URL.revokeObjectURL(objectUrl);
    }, 60000);
}

function renderDownloads() {
    const activeJobs = state.downloads.filter((job) => ["preparing", "downloading"].includes(job.status));
    const active = activeJobs.length;
    if (dom.downloadCounter) dom.downloadCounter.textContent = String(active);
    if (dom.navDownloadCount) dom.navDownloadCount.textContent = String(active || state.downloads.length);

    if (dom.floatingDownload) {
        if (active > 0) {
            dom.floatingDownload.hidden = false;
            dom.floatingDownload.classList.remove("completed");
            const primary = activeJobs[0];
            if (dom.floatingDownloadTitle) {
                dom.floatingDownloadTitle.textContent = active === 1 ? primary.track.title : `${active} Downloads Active`;
            }
            if (dom.floatingDownloadDesc) {
                dom.floatingDownloadDesc.textContent = primary.message || "Downloading FLAC…";
            }
        } else if (state.downloads.length && state.downloads[0].status === "completed") {
            dom.floatingDownload.hidden = false;
            dom.floatingDownload.classList.add("completed");
            if (dom.floatingDownloadTitle) dom.floatingDownloadTitle.textContent = "Download Complete";
            if (dom.floatingDownloadDesc) dom.floatingDownloadDesc.textContent = state.downloads[0].track.title;
            clearTimeout(window._downloadBadgeTimer);
            window._downloadBadgeTimer = setTimeout(() => {
                if (dom.floatingDownload && !state.downloads.some((j) => ["preparing", "downloading"].includes(j.status))) {
                    dom.floatingDownload.hidden = true;
                }
            }, 3500);
        } else {
            dom.floatingDownload.hidden = true;
        }
    }

    if (!state.downloads.length) {
        dom.downloadList.innerHTML = `<div class="mini-empty">${icon("download")}<span>Completed and active downloads appear here</span></div>`;
        return;
    }

    dom.downloadList.innerHTML = state.downloads.slice(0, 10).map((job) => {
        const activeJob = ["preparing", "downloading"].includes(job.status);
        return `<div class="download-item" data-download-id="${escapeHtml(job.id)}">
            <div class="download-top">
                <div class="download-art">${imageMarkup(job.track.image, "")}</div>
                <div class="download-copy"><strong>${escapeHtml(job.track.title)}</strong><span>${escapeHtml(job.message)}</span></div>
                ${activeJob ? `<button class="download-cancel" type="button" data-cancel-download aria-label="Cancel download">${icon("x")}</button>` : `<span class="download-state ${job.status}">${escapeHtml(job.status)}</span>`}
            </div>
            <div class="download-progress ${activeJob && !job.progress ? "indeterminate" : ""}"><span style="width:${activeJob && !job.progress ? 40 : job.progress}%"></span></div>
        </div>`;
    }).join("");
}

function handleDownloadAction(event) {
    const item = event.target.closest("[data-download-id]");
    if (!item || !event.target.closest("[data-cancel-download]")) return;
    state.downloads.find((job) => job.id === item.dataset.downloadId)?.controller.abort();
}

function addRecentSearch(query) {
    const value = query.trim();
    if (!value) return;
    state.recentSearches = [value, ...state.recentSearches.filter((item) => normalize(item) !== normalize(value))].slice(0, 5);
    localStorage.setItem("clash-recent-searches", JSON.stringify(state.recentSearches));
    renderRecents();
}

function renderRecents() {
    if (dom.recentRow) dom.recentRow.hidden = state.recentSearches.length === 0;
    if (dom.recentSearches) {
        dom.recentSearches.innerHTML = state.recentSearches.map((query) => `<button class="search-chip" type="button" data-recent="${escapeHtml(query)}">${escapeHtml(query)}</button>`).join("");
    }
}

function applyViewMode(mode) {
    state.viewMode = mode === "grid" ? "grid" : "list";
    localStorage.setItem("clash-view", state.viewMode);
    dom.searchResults?.classList.toggle("grid-mode", state.viewMode === "grid");
    document.querySelectorAll("[data-view-mode]").forEach((button) => button.classList.toggle("active", button.dataset.viewMode === state.viewMode));
}

function applyTheme(theme) {
    state.theme = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = state.theme;
    localStorage.setItem("clash-theme", state.theme);
    const iconName = state.theme === "dark" ? "sun" : "moon";
    if (dom.themeIcon) dom.themeIcon.innerHTML = `<use href="#icon-${iconName}"></use>`;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", state.theme === "dark" ? "#2d2140" : "#fff4c7");
}

async function checkConnection() {
    if (dom.connectionDot) dom.connectionDot.className = "connection-dot";
    if (dom.connectionTitle) dom.connectionTitle.textContent = "Checking service";
    try {
        const response = await fetch(api("/health"), { signal: AbortSignal.timeout(5000) });
        if (!response.ok) throw new Error();
        if (dom.connectionDot) dom.connectionDot.className = "connection-dot online";
        if (dom.connectionTitle) dom.connectionTitle.textContent = "Service online";
        if (dom.connectionSubtitle) dom.connectionSubtitle.textContent = new URL(state.apiBase).host;

        // Fetch backend public config for Turnstile site key
        fetch(api("/api/config")).then(r => r.json()).then(cfg => {
            if (cfg?.turnstile_site_key) {
                state.turnstileSiteKey = cfg.turnstile_site_key;
            }
        }).catch(() => {});
    } catch {
        if (dom.connectionDot) dom.connectionDot.className = "connection-dot offline";
        if (dom.connectionTitle) dom.connectionTitle.textContent = "Service offline";
        if (dom.connectionSubtitle) dom.connectionSubtitle.textContent = "Check server settings";
    }
}

function openSettings() {
    const quality = state.quality || "UHD";
    if (dom.qualitySetting) dom.qualitySetting.value = quality;
    document.querySelectorAll(".quality-card").forEach((card) => {
        card.classList.toggle("active", card.dataset.value === quality);
    });
    dom.settingsDialog.showModal();
}

function saveSettings(event) {
    event.preventDefault();
    if (dom.qualitySetting) {
        state.quality = dom.qualitySetting.value === "HD" ? "HD" : "UHD";
        localStorage.setItem("clash-quality", state.quality);
    }
    dom.settingsDialog.close();
    const qualityLabel = state.quality === "HD" ? "CD Lossless (16-bit / 44.1 kHz)" : "Ultra HD Master (24-bit / 192 kHz)";
    showToast("Settings saved", `Quality: ${qualityLabel}`);
}

function openPlayerSheet() {
    dom.playerSheet.hidden = false;
    document.body.style.overflow = "hidden";
}

function closePlayerSheet() {
    dom.playerSheet.hidden = true;
    document.body.style.overflow = "";
}

function setActiveNav(section, shouldScroll = true) {
    const valid = ["discover", "downloads", "queue"];
    const targetSection = valid.includes(section) ? section : "discover";
    state.activeSection = targetSection;

    // 1. Update sidebar nav items
    document.querySelectorAll(".nav-item[data-section]").forEach((item) => {
        item.classList.toggle("active", item.dataset.section === targetSection);
    });

    // 2. Switch between separate app views
    valid.forEach((s) => {
        const view = document.getElementById(`view-${s}`);
        if (view) {
            view.hidden = (s !== targetSection);
            view.classList.toggle("active", s === targetSection);
        }
    });

    if (shouldScroll) {
        window.scrollTo({ top: 0, behavior: "smooth" });
    }
}

function showToast(title, message, type = "success") {
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.innerHTML = `${icon(type === "error" ? "alert" : "check")}<div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span></div><button type="button" aria-label="Dismiss">${icon("x")}</button>`;
    const remove = () => {
        toast.classList.add("leaving");
        setTimeout(() => toast.remove(), 220);
    };
    toast.querySelector("button").addEventListener("click", remove);
    dom.toastRegion.appendChild(toast);
    setTimeout(remove, 4300);
}

let previousVolume = 80;

function isTextInput(element) {
    if (!element) return false;
    const tag = element.tagName;
    if (tag === "TEXTAREA" || tag === "SELECT") return true;
    if (tag === "INPUT") {
        const type = (element.type || "text").toLowerCase();
        return !["range", "checkbox", "radio", "button", "submit", "reset"].includes(type);
    }
    return Boolean(element.isContentEditable);
}

function seekRelative(seconds) {
    if (!dom.audio || !Number.isFinite(dom.audio.duration)) return;
    const target = Math.min(dom.audio.duration, Math.max(0, dom.audio.currentTime + seconds));
    dom.audio.currentTime = target;
    syncTimeline();
}

function adjustVolume(delta) {
    const current = Number(dom.volumeSlider.value) || Math.round(dom.audio.volume * 100);
    const next = Math.min(100, Math.max(0, current + delta));
    dom.audio.volume = next / 100;
    dom.volumeSlider.value = String(next);
    localStorage.setItem("clash-volume", String(next));
    updateRangeFill(dom.volumeSlider, next);
    showToast("Volume", `${next}%`);
}

function toggleMute() {
    if (dom.audio.volume > 0) {
        previousVolume = Number(dom.volumeSlider.value) || Math.round(dom.audio.volume * 100) || 80;
        dom.audio.volume = 0;
        dom.volumeSlider.value = "0";
        updateRangeFill(dom.volumeSlider, 0);
        showToast("Muted", "Volume 0%");
    } else {
        const restored = previousVolume || 80;
        dom.audio.volume = restored / 100;
        dom.volumeSlider.value = String(restored);
        localStorage.setItem("clash-volume", String(restored));
        updateRangeFill(dom.volumeSlider, restored);
        showToast("Unmuted", `Volume ${restored}%`);
    }
}

function handleKeyboard(event) {
    // 1. If user is currently typing in an input or text field
    if (isTextInput(document.activeElement)) {
        if (event.key === "Escape") {
            document.activeElement.blur();
        }
        return;
    }

    // 2. Escape: Dismiss open modal / player sheet
    if (event.key === "Escape") {
        if (!dom.playerSheet.hidden) {
            closePlayerSheet();
            return;
        }
        if (dom.settingsDialog.open) {
            dom.settingsDialog.close();
            return;
        }
    }

    // 3. Search Shortcut: '/'
    if (event.key === "/") {
        event.preventDefault();
        dom.searchInput.focus();
        dom.searchInput.select();
        return;
    }

    // 4. Play/Pause: Space or 'k' / 'K'
    if (event.code === "Space" || event.key === "k" || event.key === "K") {
        if (!dom.settingsDialog.open) {
            event.preventDefault();
            togglePlayback();
            return;
        }
    }

    // 5. Seek backward/forward: Left/Right arrows or 'j' / 'l'
    if (event.key === "ArrowLeft" || event.key === "j" || event.key === "J") {
        event.preventDefault();
        seekRelative(event.key.toLowerCase() === "j" ? -10 : -5);
        return;
    }
    if (event.key === "ArrowRight" || event.key === "l" || event.key === "L") {
        event.preventDefault();
        seekRelative(event.key.toLowerCase() === "l" ? 10 : 5);
        return;
    }

    // 6. Volume Up/Down: Up/Down arrows
    if (event.key === "ArrowUp") {
        event.preventDefault();
        adjustVolume(5);
        return;
    }
    if (event.key === "ArrowDown") {
        event.preventDefault();
        adjustVolume(-5);
        return;
    }

    // 7. Mute: 'm' / 'M'
    if (event.key === "m" || event.key === "M") {
        event.preventDefault();
        toggleMute();
        return;
    }

    // 8. Next track: 'n' / 'N' or MediaTrackNext
    if (event.key === "n" || event.key === "N" || event.key === "MediaTrackNext") {
        event.preventDefault();
        playNext(false);
        return;
    }

    // 9. Previous track: 'p' / 'P' or MediaTrackPrevious
    if (event.key === "p" || event.key === "P" || event.key === "MediaTrackPrevious") {
        event.preventDefault();
        playPrevious();
        return;
    }

    // 10. Shuffle: 's' / 'S'
    if (event.key === "s" || event.key === "S") {
        event.preventDefault();
        toggleShuffle();
        showToast("Shuffle", state.shuffle ? "Shuffle turned on" : "Shuffle turned off");
        return;
    }

    // 11. Repeat: 'r' / 'R'
    if (event.key === "r" || event.key === "R") {
        event.preventDefault();
        cycleRepeat();
        showToast("Repeat", `Repeat mode: ${state.repeatMode}`);
        return;
    }
}

initialize();
