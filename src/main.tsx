import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Metadata = { title: string; artist: string; album?: string; duration?: number; isrc?: string; url?: string; unsupported?: boolean };
type Candidate = { asset_id: string; filename: string; conflict: number; reason: string;
  meta: Metadata & { codec: string; bitrate: number; low_quality: boolean; suffix?: string;
    provenance?: { provider?: string; page?: string } } };
type TrackState = "ready" | "review" | "missing" | "working";
type Track = { id: string; position: number; state: TrackState; reason?: string; meta: Metadata; candidates: Candidate[];
  export_included: boolean | number;
  processed?: { lufs: number; dbtp: number; target_limited: boolean; needs_listening?: boolean } };
type Playlist = { id: string; name: string; url?: string; tracks: Track[]; automatic: string[] };
type Summary = { id: string; name: string; total: number };
type Settings = {
  spotify_configured: boolean; spotify_connected: boolean; soundcloud_configured: boolean; jamendo_configured: boolean;
};
type Source = { title: string; artist: string; url: string; duration: number; downloadable: boolean };

const states: Record<TrackState, string> = {
  ready: "Готово", review: "На проверке", missing: "Нет файла", working: "В работе",
};
const filters = { all: "Все", ...states };
const catalogueNames: Record<string, string> = {
  hitmos: "Hitmos", promodj: "PromoDJ", archive: "Internet Archive", openverse: "Openverse", jamendo: "Jamendo",
};
type Filter = keyof typeof filters;
const sourceLinks = (track: Metadata) => {
  const query = encodeURIComponent(track.artist + " " + track.title);
  return {
    Beatport: "https://www.beatport.com/search?q=" + query,
    Bandcamp: "https://bandcamp.com/search?q=" + query,
    SoundCloud: "https://soundcloud.com/search/sounds?q=" + query,
  };
};
const duration = (seconds?: number) => seconds
  ? Math.floor(seconds / 60) + ":" + String(Math.floor(seconds % 60)).padStart(2, "0")
  : "—";

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Проверь введённые данные.");
  return result;
}

function json(payload: unknown): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) };
}

function App() {
  const [settings, setSettings] = useState<Settings>();
  const [projects, setProjects] = useState<Summary[]>([]);
  const [trash, setTrash] = useState<Summary[] | undefined>(undefined);
  const [playlist, setPlaylist] = useState<Playlist>();
  const [tab, setTab] = useState<"spotify" | "file">("spotify");
  const [url, setUrl] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState("");
  const [selectionPending, setSelectionPending] = useState<{id: string; included: boolean}>();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState(new URLSearchParams(location.search).has("spotify")
    ? "Подключение Spotify не завершилось. Проверь адрес перенаправления и разрешённые аккаунты." : "");
  const input = useRef<HTMLInputElement>(null);
  const current = useRef("");
  const uploadTarget = useRef("");
  const active = playlist?.tracks.find(track => track.id === selected);
  const tracks = playlist?.tracks ?? [];
  const includedInZip = (track: Track) => selectionPending?.id === track.id
    ? selectionPending.included : !!track.export_included;
  const ready = tracks.filter(track => track.state === "ready").length;
  const readyToExport = tracks.filter(track => track.state === "ready" && includedInZip(track)).length;
  const review = tracks.filter(track => track.state === "review").length;
  const missing = tracks.filter(track => track.state === "missing").length;
  const working = tracks.filter(track => track.state === "working").length;
  const running = working > 0;

  async function load(id: string) {
    const result = await request<Playlist>("/api/playlists/" + id);
    if (current.current === id) setPlaylist(result);
  }

  async function open(id: string) {
    current.current = id;
    setSelected("");
    setFilter("all");
    await load(id);
  }

  useEffect(() => {
    if (location.search) history.replaceState(null, "", "/");
    request<Settings>("/api/settings").then(setSettings).catch(exc => setError(exc.message));
    request<Summary[]>("/api/playlists").then(entries => {
      setProjects(entries);
      if (entries[0]) return open(entries[0].id);
    }).catch(exc => setError(exc.message));
  }, []);

  useEffect(() => {
    const id = playlist?.id;
    if (!id || !running) return;
    const poll = window.setInterval(() => load(id).catch(exc => {
      if (current.current === id) setError(exc.message);
    }), 1800);
    return () => window.clearInterval(poll);
  }, [playlist?.id, running]);

  async function action(operation: () => Promise<void>) {
    setBusy(true);
    setError("");
    setMessage("");
    try { await operation(); }
    catch (exc) { setError(exc instanceof Error ? exc.message : "Не удалось выполнить запрос."); }
    finally { setBusy(false); }
  }

  async function changeExportSelection(playlistId: string, trackId: string, included: boolean) {
    setSelectionPending({ id: trackId, included });
    try {
      await action(async () => {
        await request(`/api/tracks/${trackId}/export-selection`, { ...json({ included }), method: "PUT" });
        await load(playlistId);
      });
    } finally {
      setSelectionPending(undefined);
    }
  }

  function changeTrash(project: { id: string; name: string }, restore: boolean) {
    void action(async () => {
      await request(`/api/playlists/${project.id}/${restore ? "restore" : "trash"}`, json({}));
      if (!restore && current.current === project.id) {
        current.current = "";
        setPlaylist(undefined);
        setSelected("");
      }
      const entries = await request<Summary[]>("/api/playlists");
      setProjects(entries);
      if (trash !== undefined) setTrash(await request<Summary[]>("/api/playlists?deleted=true"));
      if (restore) await open(project.id);
      else if (!current.current && entries[0]) await open(entries[0].id);
      setMessage(restore ? `«${project.name}» восстановлен.` : `«${project.name}» перемещён в корзину.`);
    });
  }

  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void action(async () => {
      const response = tab === "spotify"
        ? await request<{id: string}>("/api/playlists/spotify", json({ url }))
        : await request<{id: string}>("/api/playlists/file", { method: "POST", body: form });
      const reused = projects.some(project => project.id === response.id);
      setProjects(await request<Summary[]>("/api/playlists"));
      await open(response.id);
      setUrl("");
      setMessage(reused ? "Открыта существующая копия плейлиста."
        : tab === "spotify" ? "Плейлист добавлен. Ищем аудиофайлы автоматически."
          : "Плейлист добавлен. Можно запустить автоматический поиск.");
    });
  }

  function chooseFiles(trackId = "") {
    uploadTarget.current = trackId;
    if (input.current) {
      input.current.multiple = !trackId;
      input.current.click();
    }
  }

  function addAudio(files: File[]) {
    if (!playlist || !files.length) return;
    const id = playlist.id;
    const target = uploadTarget.current;
    void action(async () => {
      const failures: string[] = [];
      let completed = 0;
      for (const file of files) {
        const form = new FormData();
        form.set("file", file);
        if (target) form.set("track_id", target);
        try {
          await request("/api/playlists/" + id + "/audio", { method: "POST", body: form });
          completed += 1;
        } catch (exc) { failures.push(file.name + " — " + (exc as Error).message); }
        setMessage(`Загрузка ${completed + failures.length} из ${files.length}`);
      }
      await load(id);
      setMessage(`Файлов добавлено: ${completed} из ${files.length}. Проверь найденные совпадения.`);
      if (failures.length) setError(failures.join("\n"));
    });
  }

  const shown = tracks.filter(track => filter === "all" || track.state === filter);

  return <>
    <header className="top"><div className="wrap top-inner">
      <a href="/" className="brand"><span className="mark" aria-hidden="true">▥</span> Setcrate</a>
      <div className="top-right">
        <span className="local"><i /> На компьютере</span>
        {settings?.spotify_configured && (settings.spotify_connected
          ? <button className="button quiet" disabled={busy} onClick={() => action(async () => {
              await request("/api/spotify", {method: "DELETE"});
              setSettings(await request<Settings>("/api/settings"));
            })}>Отключить Spotify</button>
          : <a className="button quiet" href="/auth/spotify">Подключить Spotify ↗</a>)}
      </div>
    </div></header>
    <main className="wrap">
      <section className="intro">
        <div><p className="kicker">ПОДГОТОВКА DJ-СЕТА</p><h1>Из плейлиста в сет<span>.</span></h1>
        <p className="lede">Подбери нужные версии, выровняй громкость и собери плейлист для rekordbox.</p></div>
        <div className="spec"><span>MP3</span><div><strong>320 кбит/с</strong><small>−14 LUFS · ЦЕЛЬ</small></div></div>
      </section>
      <section className="panel import">
        <div className="panel-title"><h2>Добавить плейлист</h2>
          <div className="switch" aria-label="Тип импорта">
            <button className={tab === "spotify" ? "on" : ""} aria-pressed={tab === "spotify"} onClick={() => setTab("spotify")}>Spotify</button>
            <button className={tab === "file" ? "on" : ""} aria-pressed={tab === "file"} onClick={() => setTab("file")}>CSV / JSON</button>
          </div>
        </div>
        <form onSubmit={create} className="form">
          {tab === "spotify" ? <input aria-label="Ссылка на плейлист Spotify" required autoComplete="off"
            value={url} onChange={event => setUrl(event.target.value)} placeholder="https://open.spotify.com/playlist/…" />
            : <><input aria-label="Плейлист CSV или JSON" type="file" name="file" required accept=".csv,.json" />
              <input aria-label="Название плейлиста" name="name" placeholder="Имя плейлиста (если нужно)" /></>}
          <button className="button accent" disabled={busy || (tab === "spotify" && !settings?.spotify_connected)}>
            Импортировать <span aria-hidden="true">→</span></button>
        </form>
        {tab === "spotify" && settings && !settings.spotify_connected && <p className="hint">
          {settings.spotify_configured ? "Подключи Spotify в верхней панели." : "Укажи SPOTIFY_CLIENT_ID в .env или загрузи CSV / JSON."}</p>}
        {tab === "file" && <p className="hint">Spotify Playlist JSON или CSV с колонками title и artist.</p>}
      </section>
      {error && <p className="notice problem" role="alert">{error}</p>}
      {message && <p className="notice" role="status">{message}</p>}
      <section className="workspace">
        <aside className="history">
          <h2>Плейлисты</h2>
          {projects.length ? projects.map(project =>
            <button key={project.id} disabled={busy} onClick={() => action(() => open(project.id))}
              className={playlist?.id === project.id ? "current" : ""}>
              <span>{project.name}</span><small>{project.total}</small>
            </button>) : <p>Здесь появятся импортированные плейлисты.</p>}
          <button className="trash-toggle" disabled={busy} aria-expanded={trash !== undefined}
            onClick={() => trash === undefined
              ? action(async () => setTrash(await request<Summary[]>("/api/playlists?deleted=true")))
              : setTrash(undefined)}>Корзина</button>
          {trash !== undefined && <div className="trash-list">
            {trash.length ? trash.map(project => <div className="trash-entry" key={project.id}>
              <p title={project.name}>{project.name} <small>· {project.total}</small></p>
              <button disabled={busy} onClick={() => changeTrash(project, true)}>Восстановить</button>
            </div>) : <p>Корзина пуста.</p>}
          </div>}
        </aside>
        <section className="panel library">
          {playlist ? <>
            <div className="library-head">
              <div><p className="kicker">ТРЕКИ СЕТА</p><h2>{playlist.name}</h2>
                {playlist.url && <a className="origin" href={playlist.url} target="_blank" rel="noreferrer">Открыть в Spotify ↗</a>}</div>
              <div className="actions">
                {missing + review > 0 && <button className="button" disabled={busy || running}
                  onClick={() => action(async () => {
                    await request(`/api/playlists/${playlist.id}/automatic`, json({}));
                    await load(playlist.id);
                  })}>Автопоиск</button>}
                <button className="button" disabled={busy} onClick={() => chooseFiles()}>＋ Аудиофайлы</button>
                <button className="button quiet" disabled={busy} onClick={() => changeTrash(playlist, false)}>В корзину</button>
                {running || busy ? <button className="button accent" disabled>↓ ZIP</button>
                  : <a className="button accent" download href={`/api/playlists/${playlist.id}/export`}
                    title="Аудио хранится до часа после скачивания ZIP">↓ ZIP</a>}
              </div>
            </div>
            <div className="metrics">
              <span><strong>{ready}</strong> готово</span><span><strong>{review}</strong> на проверке</span>
              <span><strong>{missing}</strong> без файла</span>{working > 0 && <span><strong>{working}</strong> в работе</span>}
              {ready !== readyToExport && <span><strong>{readyToExport}</strong> в ZIP</span>}
            </div>
            <progress max={tracks.length || 1} value={ready} aria-label="Треков готово" />
            <div className="tabs" aria-label="Фильтр треков">
              {(Object.entries(filters) as [Filter, string][]).map(([key, label]) =>
                <button key={key} className={filter === key ? "selected" : ""} aria-pressed={filter === key}
                  onClick={() => setFilter(key)}>{label}</button>)}
            </div>
            <div className="track-list">
              {!shown.length && <div className="empty small">В этой категории пока пусто.</div>}
              {shown.map(track => <div key={track.id} className={selected === track.id ? "row expanded" : "row"}>
                <div className="track-line">
                  <button className="track" aria-expanded={selected === track.id}
                    onClick={() => setSelected(selected === track.id ? "" : track.id)}>
                    <span className="index">{String(track.position).padStart(2, "0")}</span>
                    <span className="track-name"><strong>{track.meta.title}</strong><small>{track.meta.artist || "—"}</small></span>
                    <span className="time">{duration(track.meta.duration)}</span>
                    <span className={"status " + track.state}>{states[track.state]}</span>
                    <span className="arrow" aria-hidden="true">{selected === track.id ? "−" : "+"}</span>
                  </button>
                  {track.state === "ready" && <label className="export-selection" title="Включить в ZIP">
                    <input type="checkbox" disabled={busy} checked={includedInZip(track)}
                      aria-label={`Включить в ZIP: ${track.meta.title}`}
                      onChange={event => void changeExportSelection(playlist.id, track.id, event.target.checked)} />
                    ZIP
                  </label>}
                </div>
                {active?.id === track.id && <TrackDetail track={track} configured={!!settings?.soundcloud_configured}
                  jamendoConfigured={!!settings?.jamendo_configured} download={link => action(async () => {
                    await request(`/api/tracks/${track.id}/download`, json({ url: link }));
                    await load(playlist.id);
                    setMessage("Аудиофайл скачан. Раскрой позицию и проверь версию перед экспортом.");
                  })}
                  busy={busy} upload={() => chooseFiles(track.id)} reset={() => action(async () => {
                    await request(`/api/tracks/${track.id}/reset`, json({}));
                    await load(playlist.id);
                  })} choose={(asset, quality) => action(async () => {
                    await request(`/api/tracks/${track.id}/choose`, json({ asset_id: asset, quality_confirmed: quality }));
                    await load(playlist.id);
                    setMessage("Вариант выбран. После обработки он попадёт в ZIP.");
                  })} reject={() => action(async () => {
                    await request(`/api/tracks/${track.id}/reject-review`, json({}));
                    await load(playlist.id);
                  })} />}
              </div>)}
            </div>
          </> : <div className="empty"><span className="mark large" aria-hidden="true">▥</span><h2>Треклист появится здесь</h2>
            <p>Импортируй плейлист, чтобы начать поиск файлов.</p></div>}
        </section>
      </section>
      <input ref={input} hidden type="file" accept=".mp3,.flac,.wav,.aif,.aiff,.m4a,.aac,.ogg,.opus"
        onChange={event => { addAudio(Array.from(event.target.files ?? [])); event.target.value = ""; }} />
    </main>
  </>;
}

function TrackDetail({ track, configured, jamendoConfigured, busy, upload, reset, choose, reject, download }: {
  track: Track; configured: boolean; busy: boolean; upload: () => void;
  jamendoConfigured: boolean; download: (link?: string) => Promise<void>;
  reset: () => Promise<void>;
  choose: (asset: string, quality: boolean) => Promise<void>;
  reject: () => Promise<void>;
}) {
  const [quality, setQuality] = useState<Record<string, boolean>>({});
  const [sources, setSources] = useState<Source[]>();
  const [finding, setFinding] = useState(false);
  const [failure, setFailure] = useState("");
  const [preview, setPreview] = useState("");
  const [downloadLink, setDownloadLink] = useState("");
  const [downloading, setDownloading] = useState(false);
  const editable = !track.meta.unsupported && (track.state === "missing" || track.state === "review");
  async function find() {
    setFinding(true);
    setFailure("");
    try { setSources((await request<{ matches: Source[] }>(`/api/tracks/${track.id}/sources?soundcloud=true`)).matches); }
    catch (exc) { setFailure((exc as Error).message); }
    finally { setFinding(false); }
  }
  async function obtain(link?: string) {
    setDownloading(true);
    try { await download(link); }
    finally { setDownloading(false); }
  }
  return <div className="detail">
    <p className="hint">{[track.meta.album, track.meta.isrc && `ISRC ${track.meta.isrc}`].filter(Boolean).join(" · ")}</p>
    {track.meta.url && <a className="origin" href={track.meta.url} target="_blank" rel="noreferrer">Запись в Spotify ↗</a>}
    {track.reason && <p className="reason">{track.reason}</p>}
    {track.processed && <p className="measure">MP3 320 кбит/с · {track.processed.lufs} LUFS · {track.processed.dbtp} dBTP
      {track.processed.target_limited && " · Громкость ограничена пиками"}</p>}
    {track.state === "ready" && <>
      {track.processed?.needs_listening && <p className="hint">У файла нет подтверждённого ISRC. Проверь версию на слух перед скачиванием ZIP.</p>}
      <audio className="track-preview" controls preload="none" src={`/api/tracks/${track.id}/preview`}
        aria-label="Предпрослушивание готового MP3" />
    </>}
    {track.state === "ready" && <div className="source-bar">
      <button className="button quiet" disabled={busy} onClick={reset}>Отменить сопоставление</button>
    </div>}
    {editable && <>
      <div className="source-bar">
        {Object.entries(sourceLinks(track.meta)).map(([name, link]) =>
          <a className="button quiet" key={name} href={link} target="_blank" rel="noreferrer">{name} ↗</a>)}
        {configured && <button className="button quiet" disabled={finding} onClick={find}>Поиск SoundCloud</button>}
        {jamendoConfigured && <button className="button quiet" disabled={busy || !track.meta.duration}
          onClick={() => obtain()}>Скачать с Jamendo</button>}
        <button className="button" disabled={busy} onClick={upload}>＋ Файл для трека</button>
      </div>
      <form className="form remote-download" onSubmit={event => {
        event.preventDefault();
        void obtain(downloadLink.trim());
      }}>
        <input type="url" aria-label="Прямая ссылка на аудиофайл" placeholder="https://… прямая ссылка на аудиофайл"
          value={downloadLink} onChange={event => setDownloadLink(event.target.value)} maxLength={4096} required />
        <button className="button" disabled={busy}>{downloading ? "Скачивание…" : "↓ Скачать файл"}</button>
      </form>
      {failure && <p role="alert" className="reason">{failure}</p>}
      {sources && <ul className="results">{sources.length ? sources.map(source => <li key={source.url}>
        <a href={source.url} target="_blank" rel="noreferrer">{source.title} ↗</a>
        <small>{source.artist} · {duration(source.duration)}{source.downloadable && " · официальный файл"}</small>
      </li>) : <li>SoundCloud не вернул результатов.</li>}</ul>}
    </>}
    {track.state === "review" && <>
      {track.candidates.map(candidate => {
        const provenance = candidate.meta.provenance;
        const source = catalogueNames[provenance?.provider ?? ""];
        return <div className="candidate" key={candidate.asset_id}>
        <div><strong>{candidate.meta.title || candidate.filename}</strong>
          <small>{[candidate.meta.artist, candidate.meta.album, duration(candidate.meta.duration),
            candidate.meta.codec.toUpperCase(), candidate.meta.bitrate ? `${Math.round(candidate.meta.bitrate / 1000)} кбит/с` : ""].filter(Boolean).join(" · ")}</small>
          <p className={candidate.conflict ? "candidate-warning" : undefined}>{candidate.reason}</p>
          {source && (provenance?.page?.startsWith("https://")
            ? <a className="origin" href={provenance.page} target="_blank" rel="noreferrer">{source} ↗</a>
            : <small>{source}</small>)}
          {/\.(mp3|flac|wav|m4a|ogg|opus)$/i.test(candidate.meta.suffix || candidate.filename) &&
            (preview === candidate.asset_id
              ? <audio controls preload="none" src={`/api/audio/${candidate.asset_id}`} aria-label="Прослушать исходник" />
              : <button className="listen" onClick={() => setPreview(candidate.asset_id)}>Прослушать</button>)}
          {candidate.meta.low_quality && <label>
            <input type="checkbox" checked={!!quality[candidate.asset_id]}
              onChange={event => setQuality({ ...quality, [candidate.asset_id]: event.target.checked })} />
            Использовать исходник пониженного качества
          </label>}
        </div>
        <button className="button accent" disabled={busy || (candidate.meta.low_quality && !quality[candidate.asset_id])}
          onClick={() => choose(candidate.asset_id, !!quality[candidate.asset_id])}>Подтвердить версию</button>
      </div>;
      })}
      <div className="source-bar">
        <button className="button quiet" disabled={busy} onClick={reject}>Ни один не подходит</button>
      </div>
    </>}
  </div>;
}

createRoot(document.getElementById("root")!).render(<App />);
