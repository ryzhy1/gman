import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiError, get, ROLE_LABELS, send, type CurrentMetrics, type Me, type Overview } from './api';
import { ConfigView, History, InstanceTable, type Placed } from './components';
import { ServiceEditor } from './edit';
import { ClusterCard, HostCard } from './hardware';
import { date, stale } from './format';
import { Resources } from './resources';
import './style.css';

const ALL = 'Все окружения';
const PAGES = [['Сервисы', '▦'], ['Серверы и GPU', '▤'], ['Аудит', '◷']] as const;
const LINKS = [['repository', 'Репозиторий'], ['grafana', 'Grafana'], ['splunk', 'Splunk'], ['bamboo', 'Bamboo']] as const;

const AUTH_ERRORS: Record<string, string> = {
  forbidden: 'Для вашей учетной записи нет роли в GMan. Обратитесь к администратору платформы.',
  idp_unavailable: 'Сервер SSO недоступен. Попробуйте позже.',
};

/** Redirect to the backend's SSO login; refuses to loop if the session cookie did not stick. */
function ssoLogin(force: boolean): boolean {
  let last = 0;
  try { last = Number(sessionStorage.getItem('gman_sso_at') ?? 0); } catch { /* storage unavailable */ }
  if (!force && Date.now() - last < 30_000) return false;
  try { sessionStorage.setItem('gman_sso_at', String(Date.now())); } catch { /* storage unavailable */ }
  location.assign(`/api/auth/login?next=${encodeURIComponent(location.pathname)}`);
  return true;
}

type Mode = 'loading' | 'oidc' | 'token';

function Login({ mode, notice, onToken }: { mode: Mode; notice: string; onToken: (token: string) => void }) {
  const [draft, setDraft] = useState('');
  return <div className="login">
    <div className="brand"><span>G</span> GMan</div>
    <h1>Ваша инфраструктура.<br />В одном месте.</h1>
    <p>Каталог сервисов, конфигурации и история запусков.</p>
    {notice && <div role="alert" className="alert">{notice}</div>}
    {mode === 'loading' && <p className="muted">Проверка входа…</p>}
    {mode === 'oidc' && <>
      <button className="primary sso" onClick={() => ssoLogin(true)}>Войти через корпоративный SSO →</button>
      <small>Роль определяется группами вашей учетной записи: просмотр, владелец сервиса или администратор платформы.</small>
    </>}
    {mode === 'token' && <>
      <form onSubmit={e => { e.preventDefault(); onToken(draft); setDraft(''); }}>
        <label htmlFor="token">Токен стенда</label>
        <input id="token" type="password" value={draft} onChange={e => setDraft(e.target.value)} required autoComplete="off" />
        <button className="primary">Открыть платформу →</button>
      </form>
      <small>Токен хранится только в памяти вкладки. SSO на этом стенде не настроен (GMAN_OIDC_ISSUER).</small>
    </>}
  </div>;
}

function App() {
  const [token, setToken] = useState('');
  const [me, setMe] = useState<Me | null>(null);
  const [mode, setMode] = useState<Mode>('loading');
  const [notice, setNotice] = useState('');
  const [reload, setReload] = useState(0);
  const [data, setData] = useState<Overview | null>(null);
  const [metrics, setMetrics] = useState<CurrentMetrics | null>(null);
  const [error, setError] = useState('');
  const [page, setPage] = useState<string>('Сервисы');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState('');
  const [tab, setTab] = useState('Обзор');
  const [environment, setEnvironment] = useState(ALL);

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const authError = params.get('auth_error');
    const loggedOut = params.has('logged_out');
    if (authError || loggedOut) history.replaceState(null, '', location.pathname);
    (async () => {
      try {
        if (!(await get<{ oidc: boolean }>('', '/api/auth/info')).oidc) { setMode('token'); return; }
        setMode('oidc');
        try { setMe(await get<Me>('', '/api/me')); return; } catch (e) {
          if (!(e instanceof ApiError && e.status === 401)) throw e;
        }
        if (authError) setNotice(AUTH_ERRORS[authError] ?? `Не удалось войти через SSO (${authError}). Попробуйте еще раз.`);
        else if (loggedOut) setNotice('Вы вышли из GMan.');
        else if (!ssoLogin(false)) setNotice('Вход не завершился: браузер не сохранил сессию. Проверьте, что портал открыт по адресу из GMAN_OIDC_REDIRECT_URI.');
      } catch (e) {
        setMode('token');
        setNotice(`API недоступен: ${(e as Error).message}`);
      }
    })();
  }, []);

  async function signInWithToken(value: string) {
    try {
      setMe(await get<Me>(value, '/api/me'));
      setToken(value);
      setNotice('');
    } catch (e) {
      setNotice(e instanceof ApiError && e.status === 401 ? 'Неверный токен' : (e as Error).message);
    }
  }

  async function logout() {
    if (me?.auth === 'oidc') {
      try { location.assign((await send<{ redirect: string }>(me, '', 'POST', '/api/auth/logout')).redirect); }
      catch (e) { setError((e as Error).message); }
      return;
    }
    setToken(''); setMe(null); setData(null); setError('');
  }

  useEffect(() => {
    if (!me) return;
    const ctrl = new AbortController();
    async function refresh() {
      try {
        setData(await get<Overview>(token, '/api/overview', ctrl.signal));
        setError('');
        // Metrics are optional: the catalog stays usable when the store is down.
        setMetrics(await get<CurrentMetrics>(token, '/api/metrics/current', ctrl.signal)
          .catch(e => ({ enabled: true, error: (e as Error).message })));
      } catch (e) {
        if (ctrl.signal.aborted) return;
        // An expired SSO session goes back through the IdP; a bad token is just reported.
        if (e instanceof ApiError && e.status === 401 && me!.auth === 'oidc') { setMe(null); ssoLogin(false); return; }
        setError((e as Error).message);
      }
    }
    void refresh();
    const timer = setInterval(refresh, 30_000);
    return () => { ctrl.abort(); clearInterval(timer); };
  }, [me, token, reload]);

  if (!me) return <Login mode={mode} notice={notice} onToken={signInWithToken} />;

  const all: Placed[] = data?.sources.flatMap(s => s.instances.map(i => ({ ...i, source: s }))) ?? [];
  const instances = all.filter(i => environment === ALL || i.environment === environment);
  const services = (data?.services ?? []).filter(s =>
    `${s.name} ${s.id} ${s.team} ${s.owner}`.toLowerCase().includes(query.toLowerCase()));
  const service = data?.services.find(s => s.id === selected);
  const own = instances.filter(i => i.service_id === selected);
  const unknown = instances.filter(i => !data?.services.some(s => s.id === i.service_id));
  const gpuCount = (data?.sources ?? []).reduce((n, s) => n + s.gpus.length + (s.nodes ?? []).reduce((m, x) => m + x.gpu_capacity, 0), 0);
  const openService = (id: string) => { if (data?.services.some(s => s.id === id)) { setPage('Сервисы'); setSelected(id); setTab('Обзор'); } };
  const open = (p: string) => { setPage(p); setSelected(''); setQuery(''); };
  const grafana = data?.grafana_url ?? '';
  const serviceDashboard = service && grafana ? `${grafana}/d/gman-service/gman-service?var-service=${encodeURIComponent(service.id)}` : '';
  const links = service ? { repository: service.repository, grafana: service.grafana || serviceDashboard, splunk: service.splunk, bamboo: service.bamboo } : null;

  return <div className="shell">
    <aside>
      <div className="brand"><span>G</span> GMan <small>PLATFORM</small></div>
      <div className="workspace">Инфраструктура компании<small>Каталог и наблюдаемость</small></div>
      <nav>{PAGES.map(([p, icon]) =>
        <button className={page === p ? 'active' : ''} onClick={() => open(p)} key={p}><span>{icon}</span>{p}</button>)}
      </nav>
      <div className="aside-bottom">
        <span className="dot" /> <span title={me.email || me.subject}>{me.name}</span>
        <small>{ROLE_LABELS[me.role]} · {me.auth === 'oidc' ? 'SSO' : 'вход по токену'}</small>
        <button onClick={logout}>Выйти</button>
      </div>
    </aside>
    <main>
      <header><div>Рабочее пространство <span>/ {page}</span></div><span className="muted">GMan · foundation</span></header>
      <div className="content">
        <div className="title">
          <div>
            <p className="eyebrow">ОБЗОР ИНФРАСТРУКТУРЫ</p>
            <h1>{service ? service.name : page}</h1>
            <p className="muted">{service ? service.description : 'Сервисы, ресурсы и изменения в вашей инфраструктуре'}</p>
          </div>
          <label>Окружение<select value={environment} onChange={e => setEnvironment(e.target.value)}>
            {[ALL, ...new Set(all.map(i => i.environment))].map(e => <option key={e}>{e}</option>)}
          </select></label>
        </div>
        {metrics && (!metrics.enabled || metrics.error) && data && <div role="status" className="alert">
          Метрики ресурсов недоступны: {metrics.error ?? 'хранилище не настроено'}. Каталог и конфигурации показаны без них.
        </div>}
        {error && <div role="alert" className="alert">{error}. {data ? 'Показан последний полученный снимок.' : 'Проверьте API и токен.'}</div>}
        {!data ? <div className="empty">{error ? 'Данные недоступны.' : 'Загрузка каталога…'}</div> : <>
          <div className="stats">
            <div><span>Сервисы в каталоге</span><strong>{data.services.length}</strong><small>{instances.length} экземпляров в выбранном окружении</small></div>
            <div><span>Подключенные источники</span><strong>{data.sources.length}</strong><small>{data.sources.filter(stale).length} с устаревшими данными, {data.sources.filter(s => s.errors?.length).length} с ошибками сбора</small></div>
            <div><span>Видеокарты</span><strong>{gpuCount}</strong><small>Docker-хосты и емкость узлов Kubernetes</small></div>
            <div><span>Требуют привязки</span><strong>{unknown.length}</strong><small>Экземпляры без записи в каталоге</small></div>
          </div>

          {page === 'Сервисы' && !service && <section>
            <div className="section-head">
              <h2>Каталог сервисов <span className="count">{services.length}</span></h2>
              <input aria-label="Поиск сервисов" placeholder="Найти сервис, команду, владельца…" value={query} onChange={e => setQuery(e.target.value)} />
            </div>
            <div className="table-wrap"><table>
              <thead><tr><th>Сервис</th><th>Команда / владелец</th><th>Экземпляры</th><th>Репозиторий</th></tr></thead>
              <tbody>{services.map(s => <tr key={s.id}>
                <td><button className="service-link" onClick={() => { setSelected(s.id); setTab('Обзор'); }}>{s.name} ↗</button><small>{s.description}</small></td>
                <td>{s.team || 'Не указана'}<small>{s.owner || 'Не указан'}</small></td>
                <td>{instances.filter(i => i.service_id === s.id).length}</td>
                <td>{s.repository ? <a href={s.repository} target="_blank" rel="noreferrer">Открыть ↗</a> : 'Не задан'}</td>
              </tr>)}</tbody>
            </table></div>
            {!services.length && <div className="empty">
              <h3>{query ? 'Сервисы не найдены' : 'Каталог готов к подключению'}</h3>
              <p>Зарегистрируйте сервис через API и задайте gman.service в Docker labels или labels шаблона Pod.</p>
            </div>}
            {unknown.length > 0 && <><h3 className="subheading">Не привязаны к каталогу</h3><InstanceTable rows={unknown} metrics={metrics} /></>}
          </section>}

          {service && <>
            <button className="back" onClick={() => setSelected('')}>← Все сервисы</button>
            <div className="links">{LINKS.map(([k, label]) => links![k]
              ? <a key={k} href={links![k]} target="_blank" rel="noreferrer">{label} ↗</a>
              : <span key={k} title="Ссылка не задана в каталоге">{label} · не настроен</span>)}
            </div>
            <div className="tabs">{['Обзор', 'Ресурсы', 'Экземпляры', 'Конфигурация', 'История'].map(t =>
              <button key={t} className={tab === t ? 'chosen' : ''} onClick={() => setTab(t)}>{t}</button>)}
            </div>
            <section>
              {tab === 'Обзор' && <>
                <h2>О сервисе</h2>
                <dl>{[['Команда', service.team], ['Ответственный', service.owner], ['Владельцы (изменяют запись)', (service.owners ?? []).join(', ')], ['Идентификатор', service.id], ['Репозиторий', service.repository]]
                  .map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v || 'Не указано'}</dd></div>)}</dl>
                <ServiceEditor key={service.id} service={service} me={me} token={token} onSaved={() => setReload(n => n + 1)} />
                <h3 className="subheading">Текущее размещение</h3>
                <InstanceTable rows={own} metrics={metrics} />
                <p className="note">Сервисы обнаруживаются независимо от способа запуска. Владелец сервиса и инициатор деплоя — разные роли. Bamboo сообщает автора деплоя; для ручных запусков без отдельного аудита автор неизвестен. CPU — в ядрах (1.0 = одно ядро), процент — от лимита контейнера. VRAM — видеопамять процессов контейнера, а не загрузка GPU.</p>
              </>}
              {tab === 'Ресурсы' && <Resources token={token} instances={own} grafanaLink={links!.grafana} />}
              {tab === 'Экземпляры' && <InstanceTable rows={own} metrics={metrics} />}
              {tab === 'Конфигурация' && <>
                {own.map(i => <ConfigView key={i.source.source_id + i.id} instance={i} />)}
                {!own.length && <div className="empty">Конфигурации еще не получены.</div>}
                <p className="note">Значения переменных окружения, команды и команды проверок здоровья не собираются.</p>
              </>}
              {tab === 'История' && <History token={token} serviceId={service.id} refreshKey={data.server_time} />}
            </section>
          </>}

          {page === 'Серверы и GPU' && <div className="host-grid">
            {data.sources.map(s => s.source_type === 'kubernetes-cluster'
              ? <ClusterCard key={s.source_id} source={s} metrics={metrics} />
              : <HostCard key={s.source_id} source={s} metrics={metrics} grafanaUrl={grafana} onService={openService} />)}
            {!data.sources.length && <section className="empty">Источники еще не подключены. Начните с одного Docker-хоста или Kubernetes-кластера.</section>}
          </div>}

          {page === 'Аудит' && <section><h2>Журнал событий</h2><History token={token} refreshKey={data.server_time} /></section>}

          <footer>Обновление каждые 30 секунд · Получено: {date(data.server_time)} · Время браузера: {Intl.DateTimeFormat().resolvedOptions().timeZone}</footer>
        </>}
      </div>
    </main>
  </div>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
