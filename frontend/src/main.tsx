import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiError, get, ROLE_LABELS, send, type CurrentMetrics, type Me, type Overview, type SplunkLinks } from './api';
import { Alerts } from './alerts';
import { ConfigView, History, InstanceTable, instanceHref, placeInstances, serverAddress, servicePorts, type Placed } from './components';
import { ConfigHistory } from './configs';
import { ServiceEditor } from './edit';
import { ClusterCard, HostCard } from './hardware';
import { date, grafanaLink, stale, syncServerClock } from './format';
import { Releases } from './releases';
import { Resources } from './resources';
import { Workloads } from './workloads';
import { LaunchDetails } from './launch';
import { LlmCharts, LlmSummary } from './llm';
import { Activity, AlertTriangle, ArrowLeft, ArrowRight, Boxes, ChevronRight, ExternalLink, History as HistoryIcon, LogOut, Menu, Moon, Search, Server, Sun } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Avatar, AvatarFallback } from '@/ui/avatar';
import { Badge } from '@/ui/badge';
import { Button, buttonVariants } from '@/ui/button';
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/ui/card';
import { Input } from '@/ui/input';
import { Label } from '@/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/ui/select';
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from '@/ui/sheet';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/ui/table';
import { Tabs, TabsList, TabsTrigger } from '@/ui/tabs';
import './style.css';

const ALL = 'Все окружения';
const PAGES = [['Сервисы', Boxes], ['Серверы и GPU', Server], ['Алерты', AlertTriangle], ['Аудит', HistoryIcon]] as const;
const TABS = ['Обзор', 'Ресурсы', 'Экземпляры', 'Конфигурация', 'Релизы', 'История'];
const LINKS = [['repository', 'Репозиторий'], ['grafana', 'Grafana'], ['splunk', 'Логи в Splunk'], ['bamboo', 'Bamboo']] as const;

function route() {
  try { return location.hash.slice(1).split('/').map(decodeURIComponent); }
  catch { return []; }
}

const serviceHref = (id: string) => `#services/${encodeURIComponent(id)}`;

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
  location.assign(`/api/auth/login?next=${encodeURIComponent(location.pathname + location.hash)}`);
  return true;
}

type Mode = 'loading' | 'oidc' | 'token';

/** Light/dark theme: the viewer's choice is remembered in this browser, the system setting otherwise. */
function useTheme() {
  const [dark, setDark] = useState(() => {
    try { const saved = localStorage.getItem('gman_theme'); if (saved) return saved === 'dark'; } catch { /* storage unavailable */ }
    return matchMedia('(prefers-color-scheme: dark)').matches;
  });
  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark);
    try { localStorage.setItem('gman_theme', dark ? 'dark' : 'light'); } catch { /* storage unavailable */ }
  }, [dark]);
  return [dark, () => setDark(d => !d)] as const;
}

function Brand() {
  return <div className="flex items-center gap-2.5">
    <span className="grid size-8 place-items-center rounded-lg bg-primary text-sm font-bold text-primary-foreground">G</span>
    <span className="text-lg font-semibold tracking-tight">GMan</span>
    <Badge variant="muted" className="text-[10px] tracking-wider">PLATFORM</Badge>
  </div>;
}

function Login({ mode, notice, onToken }: { mode: Mode; notice: string; onToken: (token: string) => void }) {
  const [draft, setDraft] = useState('');
  return <div className="grid min-h-svh place-items-center bg-muted/40 p-4">
    <Card className="w-full max-w-md">
      <CardHeader className="gap-4">
        <Brand />
        <CardTitle className="text-2xl leading-tight">Ваша инфраструктура.<br />В одном месте.</CardTitle>
        <CardDescription>Каталог сервисов, конфигурации и история запусков.</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        {notice && <div role="alert" className="alert">{notice}</div>}
        {mode === 'loading' && <p className="text-sm text-muted-foreground">Проверка входа…</p>}
        {mode === 'oidc' && <>
          <Button size="lg" className="w-full" onClick={() => ssoLogin(true)}>Войти через корпоративный SSO <ArrowRight /></Button>
          <p className="text-xs text-muted-foreground">Роль определяется группами вашей учетной записи: просмотр, владелец сервиса или администратор платформы.</p>
        </>}
        {mode === 'token' && <>
          <form className="grid gap-3" onSubmit={e => { e.preventDefault(); onToken(draft); setDraft(''); }}>
            <Label htmlFor="token">Токен стенда</Label>
            <Input id="token" type="password" value={draft} onChange={e => setDraft(e.target.value)} required autoComplete="off" />
            <Button size="lg">Открыть платформу <ArrowRight /></Button>
          </form>
          <p className="text-xs text-muted-foreground">Токен хранится только в памяти вкладки. SSO на этом стенде не настроен (GMAN_OIDC_ISSUER).</p>
        </>}
      </CardContent>
    </Card>
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
  // Runbook links from alert notifications open <portal>/#alerts/<name>.
  const [page, setPage] = useState<string>(() => route()[0] === 'alerts' ? 'Алерты'
    : route()[0] === 'hosts' ? 'Серверы и GPU' : route()[0] === 'audit' ? 'Аудит' : 'Сервисы');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(() => route()[0] === 'services' ? route()[1] ?? '' : '');
  const [instanceRoute, setInstanceRoute] = useState<string[]>(() => route()[0] === 'instances' ? route().slice(1) : []);
  const [tab, setTab] = useState('Обзор');
  const [environment, setEnvironment] = useState(ALL);
  const [release, setRelease] = useState<number | null>(null);
  const [range, setRange] = useState('1h');
  const [splunk, setSplunk] = useState<SplunkLinks | null>(null);
  const [dark, toggleTheme] = useTheme();
  const [menu, setMenu] = useState(false);

  useEffect(() => {
    const navigate = () => {
      const [kind, ...ids] = route();
      setSelected(kind === 'services' ? ids[0] ?? '' : '');
      setInstanceRoute(kind === 'instances' ? ids : []);
      setPage(kind === 'alerts' ? 'Алерты' : kind === 'hosts' ? 'Серверы и GPU' : kind === 'audit' ? 'Аудит' : 'Сервисы');
      setTab('Обзор'); setRelease(null);
    };
    window.addEventListener('hashchange', navigate);
    return () => window.removeEventListener('hashchange', navigate);
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const authError = params.get('auth_error');
    const loggedOut = params.has('logged_out');
    if (authError || loggedOut) history.replaceState(null, '', location.pathname);
    (async () => {
      try {
        const info = await get<{ oidc: boolean; open?: boolean }>('', '/api/auth/info');
        if (info.open) { setMe(await get<Me>('', '/api/me')); return; }
        if (!info.oidc) { setMode('token'); return; }
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
        const overview = await get<Overview>(token, '/api/overview', ctrl.signal);
        syncServerClock(overview.server_time);
        setData(overview);
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

  // Links are built by the API (SPL escaping); refreshed with the overview so new instances get one.
  useEffect(() => setSplunk(null), [selected, environment, range]);
  useEffect(() => {
    if (!me || !selected) return;
    const ctrl = new AbortController();
    const params = new URLSearchParams({ service_id: selected, range, ...(environment === ALL ? {} : { environment }) });
    get<SplunkLinks>(token, `/api/logs/splunk?${params}`, ctrl.signal).then(setSplunk).catch(() => undefined);
    return () => ctrl.abort();
  }, [me, token, selected, environment, range, data?.server_time]);

  if (!me) return <Login mode={mode} notice={notice} onToken={signInWithToken} />;

  const all: Placed[] = placeInstances(data?.sources ?? []);
  const instances = all.filter(i => environment === ALL || i.environment === environment);
  const services = (data?.services ?? []).filter(s =>
    `${s.name} ${s.id} ${s.team} ${s.owner}`.toLowerCase().includes(query.toLowerCase()));
  // One row per server and ports of a service's running instances in the chosen environment.
  // One row per server and ports of a service's running instances in the chosen environment; running
  // containers and pods without a catalog entry follow, each opening its own instance page.
  type Row = { key: string; name: string; href: string; address: string; ports: string; first: boolean; unbound?: boolean };
  const needle = query.toLowerCase();
  const running: Row[] = services.flatMap(service => {
    const rows = new Map<string, { address: string; ports: string }>();
    for (const i of instances) if (i.service_id === service.id && i.status === 'running') {
      const row = { address: serverAddress(i), ports: servicePorts(i).join(', ') };
      rows.set(`${row.address}|${row.ports}`, row);
    }
    return [...rows.values()].sort((a, b) => a.address.localeCompare(b.address)).map((r, n) =>
      ({ ...r, key: `${service.id}/${r.address}/${r.ports}`, name: service.name, href: serviceHref(service.id), first: n === 0 }));
  }).concat(instances
    .filter(i => i.status === 'running' && !data?.services.some(s => s.id === i.service_id)
      && `${i.name} ${i.service_id} ${i.controller}`.toLowerCase().includes(needle))
    .sort((a, b) => a.name.localeCompare(b.name))
    .map(i => ({ key: `${i.source.source_id}/${i.id}`, name: i.name, href: instanceHref(i), first: true, unbound: true,
      address: serverAddress(i), ports: servicePorts(i).join(', ') })));
  const runningCount = new Set(running.filter(r => r.first).map(r => r.key)).size;
  const service = data?.services.find(s => s.id === selected);
  const selectedInstance = all.find(i => i.source.source_id === instanceRoute[0] && i.id === instanceRoute[1]);
  const own = instances.filter(i => i.service_id === selected);
  const unknown = instances.filter(i => !data?.services.some(s => s.id === i.service_id));
  // Node-local GPU sources are shown inside their cluster's node; without that cluster, as a host of their own.
  const hostSources = (data?.sources ?? []).filter(s => s.source_type !== 'kubernetes-node'
    || !data?.sources.some(c => c.source_type === 'kubernetes-cluster' && c.source_id === s.cluster));
  // GPUs of node-local sources are already counted in their cluster's node capacity, unless that cluster is absent.
  const gpuCount = hostSources.reduce((n, s) => n + s.gpus.length
    + (s.nodes ?? []).reduce((m, x) => m + x.gpu_capacity, 0), 0);
  const openService = (id: string) => { if (data?.services.some(s => s.id === id)) location.hash = serviceHref(id); };
  const openRelease = (id: number, serviceId: string) => {
    if (!data?.services.some(s => s.id === serviceId)) return;
    history.pushState(null, '', serviceHref(serviceId));
    setInstanceRoute([]); setPage('Сервисы'); setSelected(serviceId); setTab('Релизы'); setRelease(id);
  };
  const open = (p: string) => {
    location.hash = p === 'Алерты' ? '#alerts' : p === 'Серверы и GPU' ? '#hosts' : p === 'Аудит' ? '#audit' : '#services';
    setPage(p); setSelected(''); setInstanceRoute([]); setQuery(''); setMenu(false);
  };
  const grafana = data?.grafana_url ?? '';
  // The card's environment and period travel to the dashboard; an explicit catalog link is used as is.
  const serviceDashboard = service
    ? grafanaLink(grafana, 'gman-service', { service: service.id, environment: environment === ALL ? null : environment }, range) : '';
  const links = service ? { repository: service.repository, grafana: service.grafana || serviceDashboard, splunk: service.splunk || splunk?.service || '', bamboo: service.bamboo } : null;

  const nav = <nav className="grid gap-1">{PAGES.map(([p, Icon]) =>
    <Button key={p} variant={page === p ? 'secondary' : 'ghost'} onClick={() => open(p)}
      className={cn('justify-start gap-3', page === p ? 'font-semibold text-primary' : 'text-muted-foreground')}>
      <Icon className="size-4" />{p}
    </Button>)}
  </nav>;
  const account = <div className="flex items-center gap-3 rounded-lg border bg-background p-3">
    <Avatar className="size-8"><AvatarFallback className="text-xs">{me.name.slice(0, 2).toUpperCase()}</AvatarFallback></Avatar>
    <div className="min-w-0 flex-1">
      <p className="truncate text-sm font-medium" title={me.email || me.subject}>{me.name}</p>
      <p className="truncate text-xs text-muted-foreground">{ROLE_LABELS[me.role]} · {me.auth === 'oidc' ? 'SSO' : me.auth === 'open' ? 'без входа' : 'вход по токену'}</p>
    </div>
    {me.auth !== 'open' && <Button variant="ghost" size="icon-sm" onClick={logout} title="Выйти" aria-label="Выйти"><LogOut /></Button>}
  </div>;
  const stats = data ? [
    ['Сервисы в каталоге', data.services.length, `${instances.length} экземпляров в выбранном окружении`, Boxes],
    ['Подключенные источники', data.sources.length, `${data.sources.filter(stale).length} с устаревшими данными, ${data.sources.filter(s => s.errors?.length).length} с ошибками сбора`, Server],
    ['Видеокарты', gpuCount, 'Docker-хосты и емкость узлов Kubernetes', Activity],
    ['Требуют привязки', unknown.length, 'Экземпляры без записи в каталоге', AlertTriangle],
  ] as const : [];

  return <div className="min-h-svh bg-muted/40">
    <aside className="fixed inset-y-0 left-0 z-20 hidden w-60 flex-col gap-6 border-r bg-sidebar p-4 md:flex">
      <div className="px-2 pt-2"><Brand /></div>
      <div className="px-2">
        <p className="text-sm font-medium">Инфраструктура компании</p>
        <p className="text-xs text-muted-foreground">Каталог и наблюдаемость</p>
      </div>
      {nav}
      <div className="mt-auto">{account}</div>
    </aside>
    <div className="md:pl-60">
      <header className="sticky top-0 z-10 flex h-14 items-center gap-3 border-b bg-background/80 px-4 backdrop-blur md:px-8">
        <Sheet open={menu} onOpenChange={setMenu}>
          <SheetTrigger asChild><Button variant="ghost" size="icon-sm" className="md:hidden" aria-label="Меню"><Menu /></Button></SheetTrigger>
          <SheetContent side="left" className="w-64 gap-6 p-4">
            <SheetTitle className="px-2 pt-2"><Brand /></SheetTitle>
            {nav}
            <div className="mt-auto">{account}</div>
          </SheetContent>
        </Sheet>
        <nav aria-label="Навигация" className="flex min-w-0 items-center gap-1.5 text-sm text-muted-foreground">
          <span className="hidden sm:inline">Рабочее пространство</span>
          <ChevronRight className="hidden size-4 sm:block" />
          <span className="truncate font-medium text-foreground">{page}</span>
        </nav>
        <div className="ml-auto flex items-center gap-2">
          <span className="hidden text-xs text-muted-foreground sm:inline">GMan · foundation</span>
          <Button variant="ghost" size="icon-sm" onClick={toggleTheme} aria-label={dark ? 'Светлая тема' : 'Тёмная тема'}>
            {dark ? <Sun /> : <Moon />}
          </Button>
        </div>
      </header>
      <main className="content mx-auto w-full max-w-[1600px] space-y-6 p-4 md:p-8">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div className="min-w-0 space-y-1">
            <p className="text-xs font-medium tracking-widest text-muted-foreground">ОБЗОР ИНФРАСТРУКТУРЫ</p>
            <h1 className="truncate text-2xl font-semibold tracking-tight md:text-3xl">{selectedInstance?.name ?? (service ? service.name : page)}</h1>
            <p className="text-sm text-muted-foreground">{service ? service.description : 'Сервисы, ресурсы и изменения в вашей инфраструктуре'}</p>
          </div>
          <div className="grid gap-1.5">
            <Label className="text-xs text-muted-foreground">Окружение</Label>
            <Select value={environment} onValueChange={setEnvironment}>
              <SelectTrigger className="w-full bg-background sm:w-52"><SelectValue /></SelectTrigger>
              <SelectContent>{[ALL, ...new Set(all.map(i => i.environment))].map(e => <SelectItem key={e} value={e}>{e}</SelectItem>)}</SelectContent>
            </Select>
          </div>
        </div>
        {metrics && (!metrics.enabled || metrics.error) && data && <div role="status" className="alert">
          Метрики ресурсов недоступны: {metrics.error ?? 'хранилище не настроено'}. Каталог и конфигурации показаны без них.
        </div>}
        {error && <div role="alert" className="alert">{error}. {data ? 'Показан последний полученный снимок.' : 'Проверьте API и токен.'}</div>}
        {!data ? <div className="empty">{error ? 'Данные недоступны.' : 'Загрузка каталога…'}</div> : <>
          <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
            {stats.map(([label, n, hint, Icon]) => <Card key={label} className="gap-2 py-5">
              <CardHeader className="px-5">
                <CardDescription>{label}</CardDescription>
                <CardAction><Icon className="size-4 text-muted-foreground" /></CardAction>
                <CardTitle className="text-3xl font-semibold tabular-nums">{n}</CardTitle>
              </CardHeader>
              <CardContent className="px-5 text-xs text-muted-foreground">{hint}</CardContent>
            </Card>)}
          </div>

          {page === 'Сервисы' && !selected && !instanceRoute.length && <Card>
            <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div className="space-y-1.5">
                <CardTitle className="flex items-center gap-2">Запущенные сервисы <Badge variant="muted">{runningCount}</Badge></CardTitle>
                <CardDescription>Нажмите на строку, чтобы открыть страницу сервиса</CardDescription>
              </div>
              <div className="relative w-full sm:w-72">
                <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input aria-label="Поиск сервисов" placeholder="Найти сервис, команду, владельца…" className="pl-8"
                  value={query} onChange={e => setQuery(e.target.value)} />
              </div>
            </CardHeader>
            <CardContent>
              {running.length > 0 && <div className="rounded-md border"><Table>
                <TableHeader><TableRow className="bg-muted/50 hover:bg-muted/50">
                  <TableHead>Сервис</TableHead><TableHead>IP сервера</TableHead><TableHead>Порт</TableHead><TableHead className="w-8" />
                </TableRow></TableHeader>
                <TableBody>{running.map(r =>
                  <TableRow key={r.key} tabIndex={0} title={r.unbound ? 'Открыть экземпляр' : 'Открыть сервис'}
                    className={cn('group cursor-pointer focus-visible:bg-muted focus-visible:outline-none', !r.first && 'border-t-0')}
                    onClick={() => { if (!window.getSelection()?.toString()) location.hash = r.href; }}
                    onKeyDown={e => { if (e.key === 'Enter') location.hash = r.href; }}>
                    <TableCell className="font-medium">{r.first && <>{r.name}
                      {r.unbound && <Badge variant="muted" className="ml-2 font-normal" title="Нет записи в каталоге и метки gman.service">не в каталоге</Badge>}</>}</TableCell>
                    <TableCell className="font-mono text-xs">{r.address || '—'}</TableCell>
                    <TableCell className="font-mono text-xs">{r.ports || '—'}</TableCell>
                    <TableCell><ChevronRight className="size-4 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" /></TableCell>
                  </TableRow>)}</TableBody>
              </Table></div>}
              {!running.length && <div className="empty">
                <h3>{query ? 'Сервисы не найдены' : 'Нет запущенных сервисов'}</h3>
                <p>Зарегистрируйте сервис через API и задайте gman.service в Docker labels или labels шаблона Pod.</p>
              </div>}
            </CardContent>
          </Card>}

          {page === 'Сервисы' && (selected || instanceRoute.length > 0) &&
            <a className={cn(buttonVariants({ variant: 'ghost', size: 'sm' }), '-ml-3 text-muted-foreground')} href="#services"><ArrowLeft /> Все сервисы</a>}
          {page === 'Сервисы' && ((selected && !service) || (instanceRoute.length > 0 && !selectedInstance)) &&
            <section className="empty">Сервис или экземпляр не найден в текущем снимке. Возможно, он уже удалён.</section>}
          {selectedInstance && <section>
            <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
              <h2>Запуск и конфигурация</h2>
              {data.services.some(s => s.id === selectedInstance.service_id) &&
                <a className={buttonVariants({ variant: 'outline', size: 'sm' })} href={serviceHref(selectedInstance.service_id)}>Открыть сервис <ArrowRight /></a>}
            </div>
            <LaunchDetails instance={selectedInstance} events={data.events} expanded grafanaUrl={grafana} range={range}
              serviceGrafana={data.services.find(s => s.id === selectedInstance.service_id)?.grafana} />
          </section>}
          {service && <div className="space-y-4">
            <div className="flex flex-wrap gap-2">{LINKS.map(([k, label]) => links![k]
              ? <a key={k} className={buttonVariants({ variant: 'outline', size: 'sm' })} href={links![k]} target="_blank" rel="noopener noreferrer">{label} <ExternalLink /></a>
              : k === 'splunk' ? null : <Badge key={k} variant="muted" className="h-8 rounded-md px-3" title="Ссылка не задана в каталоге">{label} · не настроен</Badge>)}
            </div>
            <Tabs value={tab} onValueChange={t => { setTab(t); setRelease(null); }}>
              <TabsList className="h-auto max-w-full justify-start overflow-x-auto">{TABS.map(t => <TabsTrigger key={t} value={t}>{t}</TabsTrigger>)}</TabsList>
            </Tabs>
            <section>
              {tab === 'Обзор' && <>
                <h2>О сервисе</h2>
                <dl>{[['Команда', service.team], ['Ответственный', service.owner], ['Владельцы (изменяют запись)', (service.owners ?? []).join(', ')], ['Идентификатор', service.id], ['Репозиторий', service.repository]]
                  .map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v || 'Не указано'}</dd></div>)}</dl>
                <ServiceEditor key={service.id} service={service} me={me} token={token} onSaved={() => setReload(n => n + 1)} />
                <LlmSummary metrics={metrics} serviceId={service.id} grafanaUrl={grafana} range={range} />
                <h3 className="subheading">Кем, когда и с какой конфигурацией запущен</h3>
                {own.map(i => <LaunchDetails key={i.source.source_id + i.id} instance={i} events={data.events}
                  grafanaUrl={grafana} range={range} serviceGrafana={service.grafana} />)}
                {!own.length && <p className="empty">Агент ещё не передал экземпляры этого сервиса в выбранном окружении.</p>}
                <h3 className="subheading">Текущее размещение</h3>
                <InstanceTable rows={own} metrics={metrics} logs={splunk?.instances} />
                <Workloads token={token} serviceId={service.id} environment={environment === ALL ? undefined : environment}
                  grafanaUrl={grafana} range={range} refreshKey={data.server_time} />
                <p className="note">Сервисы обнаруживаются независимо от способа запуска. Владелец сервиса и инициатор деплоя — разные роли. Bamboo сообщает автора деплоя; для ручных запусков без отдельного аудита автор неизвестен. CPU — в ядрах (1.0 = одно ядро), процент — от лимита контейнера. VRAM — видеопамять процессов контейнера, а не загрузка GPU.</p>
              </>}
              {tab === 'Ресурсы' && <>
                <Resources token={token} instances={own} grafanaLink={links!.grafana} range={range} onRange={setRange}
                  splunkLink={splunk?.service || links!.splunk} />
                <LlmCharts token={token} metrics={metrics} serviceId={service.id} range={range} />
              </>}
              {tab === 'Экземпляры' && <InstanceTable rows={own} metrics={metrics} logs={splunk?.instances} />}
              {tab === 'Конфигурация' && <>
                <h2>Текущая конфигурация</h2>
                {own.map(i => <ConfigView key={i.source.source_id + i.id} instance={i} />)}
                {!own.length && <div className="empty">Конфигурации еще не получены.</div>}
                <p className="note">Значения переменных окружения, команды и команды проверок здоровья не собираются.</p>
                <h2 className="subheading">История версий</h2>
                <ConfigHistory key={service.id} token={token} serviceId={service.id}
                  instances={all.filter(i => i.service_id === service.id)} refreshKey={data.server_time} />
              </>}
              {tab === 'Релизы' && <Releases token={token} serviceId={service.id} environment={environment === ALL ? undefined : environment}
                refreshKey={data.server_time} selected={release} onSelect={setRelease} />}
              {tab === 'История' && <History token={token} serviceId={service.id} refreshKey={data.server_time} onRelease={openRelease} />}
            </section>
          </div>}

          {page === 'Серверы и GPU' && <div className="host-grid">
            {hostSources.map(s => s.source_type === 'kubernetes-cluster'
              ? <ClusterCard key={s.source_id} source={s} sources={data.sources} metrics={metrics} grafanaUrl={grafana} range={range} onService={openService}>
                <Workloads token={token} cluster={s.source_id} grafanaUrl={grafana} range={range} refreshKey={data.server_time} />
              </ClusterCard>
              : <HostCard key={s.source_id} source={s} metrics={metrics} grafanaUrl={grafana} range={range} onService={openService} />)}
            {!data.sources.length && <section className="empty">Источники еще не подключены. Начните с одного Docker-хоста или Kubernetes-кластера.</section>}
          </div>}

          {page === 'Алерты' && <Alerts token={token} refreshKey={data.server_time} />}

          {page === 'Аудит' && <section><h2>Журнал событий</h2><History token={token} refreshKey={data.server_time} onRelease={openRelease} /></section>}

          <footer className="pb-4 text-xs text-muted-foreground">Обновление каждые 30 секунд · Получено: {date(data.server_time)} · Время браузера: {Intl.DateTimeFormat().resolvedOptions().timeZone}</footer>
        </>}
      </main>
    </div>
  </div>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
