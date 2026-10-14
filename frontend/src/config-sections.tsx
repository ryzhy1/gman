import type { ReactNode } from 'react';
import type { Placed } from './components';
import { bytes } from './format';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/ui/table';
import { tone as badge } from '@/ui/tone';

const list = (v: unknown): string[] => Array.isArray(v) ? v.map(String).filter(Boolean) : [];
const text = (v: unknown) => v == null || v === '' ? '' : String(v);
const yes = (v: unknown, label: string) => v
  ? <span className={badge('warn')}>{label}</span> : <span className="muted">нет</span>;

/** One labelled row; empty values are dropped unless `fallback` says what their absence means. */
type Row = [label: string, value: ReactNode, fallback?: string];

function Fields({ rows }: { rows: Row[] }) {
  const shown = rows.filter(([, v, fallback]) => (v !== '' && v != null && v !== false) || fallback);
  if (!shown.length) return null;
  return <dl className="config-fields">{shown.map(([label, v, fallback]) => <div key={label}>
    <dt>{label}</dt><dd>{v !== '' && v != null && v !== false ? v : <span className="muted">{fallback}</span>}</dd>
  </div>)}</dl>;
}

function Chips({ items, mono = true }: { items: string[]; mono?: boolean }) {
  return <span className="chips">{items.map(i => <code key={i} className={mono ? '' : 'font-sans'}>{i}</code>)}</span>;
}

function Group({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return <div className="config-group">
    <h4>{title}{count != null && <span className="count">{count}</span>}</h4>
    {children}
  </div>;
}

/** Docker: "0.0.0.0:8080->80/tcp"; Kubernetes: "80/TCP" (container port only). */
function parsePort(p: string) {
  const [host, container] = p.includes('->') ? p.split('->') : ['', p];
  const [port, protocol = ''] = container.split('/');
  const hostPort = host.split(':').pop() ?? '';
  const hostIp = host.slice(0, host.length - hostPort.length - 1);
  return { hostIp, hostPort, port, protocol: protocol.toLowerCase() };
}

/** Docker: "volume data -> /var/lib (ro)"; Kubernetes: "data -> /var/lib (ro)". */
function parseMount(m: string, docker: boolean) {
  const readonly = m.endsWith(' (ro)');
  const [left, destination = ''] = (readonly ? m.slice(0, -5) : m).split(' -> ');
  const space = left.indexOf(' ');
  const [type, source] = docker && space > 0 ? [left.slice(0, space), left.slice(space + 1)] : ['', left];
  return { type, source, destination, readonly };
}

/** Docker device request is collected as JSON, GPU environment as "NAME=value"; pod limits as "nvidia.com/gpu=1". */
function splitGpu(items: string[], docker: boolean) {
  const devices: string[] = [], env: [string, string][] = [], limits: string[] = [];
  for (const item of items) {
    if (item.startsWith('{')) {
      try {
        const r = JSON.parse(item) as { driver?: string; ids?: string[] | null; count?: number | null };
        devices.push([r.driver || 'default', r.count === -1 ? 'все GPU' : r.count ? `${r.count} шт.` : '',
          r.ids?.length ? `ID ${r.ids.join(', ')}` : ''].filter(Boolean).join(' · '));
      } catch { devices.push(item); }
    } else if (docker && item.includes('=')) {
      const at = item.indexOf('=');
      env.push([item.slice(0, at), item.slice(at + 1)]);
    } else limits.push(item);
  }
  return { devices, env, limits };
}

const KNOWN = new Set(['image', 'image_id', 'runtime', 'cpu_request', 'cpu_limit', 'cpuset', 'memory_request_bytes',
  'memory_limit_bytes', 'shm_size_bytes', 'gpu_requests', 'ports', 'networks', 'network_mode', 'mounts',
  'restart_policy', 'probes', 'user', 'log_driver', 'privileged', 'readonly_rootfs', 'cap_add', 'cap_drop',
  'pid_mode', 'ipc_mode', 'ulimits', 'env', 'env_files', 'command', 'args']);

/** A token as it would be typed in a shell: quoted when it has spaces, quotes or is empty. */
const shellWord = (s: string) => /^[\w@%+=:,./-]+$/.test(s) ? s : `'${s.replace(/'/g, `'\\''`)}'`;

/** argv as parameter rows: "--name=value", "--name value", a bare flag, or a positional argument.
 *  The program itself (a first token that is not an option) is not a parameter. */
function parseArgs(argv: string[]): [string, string][] {
  const rows: [string, string][] = [];
  for (let n = argv.length && !argv[0].startsWith('-') ? 1 : 0; n < argv.length; n++) {
    const token = argv[n];
    if (!token.startsWith('-') || token === '-' || token === '--') rows.push(['', token]);
    else if (token.includes('=')) rows.push([token.slice(0, token.indexOf('=')), token.slice(token.indexOf('=') + 1)]);
    else if (n + 1 < argv.length && !argv[n + 1].startsWith('-')) rows.push([token, argv[++n]]);
    else rows.push([token, '']);
  }
  return rows;
}

function LaunchTable({ rows }: { rows: [string, string][] }) {
  if (!rows.length) return null;
  return <div className="table-wrap"><Table className="dense">
    <TableHeader><TableRow><TableHead>Параметр</TableHead><TableHead>Значение</TableHead></TableRow></TableHeader>
    <TableBody>{rows.map(([k, v], n) => <TableRow key={n}>
      <TableCell className="align-top">{k ? <code className="break-all">{k}</code> : <span className="muted">аргумент</span>}</TableCell>
      <TableCell className="whitespace-normal">{v ? <code className="break-all">{v}</code> : <span className="muted">флаг</span>}</TableCell>
    </TableRow>)}</TableBody>
  </Table></div>;
}

/** "NAME=value" split at the first "="; Kubernetes references (valueFrom, envFrom) arrive as "<...>" values. */
function parseEnv(items: string[]): [string, string][] {
  return items.map(item => {
    const at = item.indexOf('=');
    return at < 0 ? [item, ''] : [item.slice(0, at), item.slice(at + 1)];
  });
}

function EnvTable({ rows }: { rows: [string, string][] }) {
  if (!rows.length) return null;
  return <div className="table-wrap"><Table className="dense">
    <TableHeader><TableRow><TableHead>Переменная</TableHead><TableHead>Значение</TableHead></TableRow></TableHeader>
    <TableBody>{rows.map(([k, v], n) => <TableRow key={n}>
      <TableCell className="align-top"><code className="break-all">{k}</code></TableCell>
      <TableCell className="whitespace-normal"><code className={v.startsWith('<') && v.endsWith('>') ? 'break-all text-muted-foreground' : 'break-all'}>{v || '""'}</code></TableCell>
    </TableRow>)}</TableBody>
  </Table></div>;
}

/** The observed config split into labelled groups instead of one JSON dump. */
export function ConfigSections({ instance }: { instance: Placed }) {
  const c = instance.config;
  const docker = instance.runtime === 'docker';
  const ports = list(c.ports).map(parsePort);
  const mounts = list(c.mounts).map(m => parseMount(m, docker));
  const gpu = splitGpu(list(c.gpu_requests), docker);
  // Snapshots from agents before 0.7 carry only the GPU selection variables (in gpu_requests).
  const collected = Array.isArray(c.env);
  const env = collected ? parseEnv(list(c.env)) : gpu.env;
  // Dotenv files the application loads itself (Docker), read by the agent from the working directory.
  const files = c.env_files && typeof c.env_files === 'object' && !Array.isArray(c.env_files)
    ? Object.entries(c.env_files as Record<string, unknown>).map(([path, items]) => [path, parseEnv(list(items))] as const) : [];
  const ulimits = list(c.ulimits).map(u => {
    const [name, range = ''] = u.split('=');
    const [soft, hard] = range.split(':');
    return { name, soft, hard };
  });
  const probes = list(c.probes);
  // Launch parameters: Docker ENTRYPOINT + CMD, Kubernetes command/args overrides; absent from agents before 0.8.
  const launched = Array.isArray(c.command) || Array.isArray(c.args);
  const argv = [...list(c.command), ...list(c.args)];
  const networks = list(c.networks);
  const extra = Object.entries(c).filter(([k, v]) => !KNOWN.has(k) && v != null && v !== '' && !(Array.isArray(v) && !v.length));
  const host = (mode: unknown) => mode === 'host' ? <span className={badge('warn')}>host</span> : text(mode);

  return <div className="config-groups">
    <Group title="Образ">
      <Fields rows={[
        ['Образ', text(c.image) && <code className="break-all">{text(c.image)}</code>, 'не указан'],
        ['Image ID', text(c.image_id) && <code className="break-all text-muted-foreground">{text(c.image_id)}</code>],
        ['Runtime', text(c.runtime)],
      ]} />
    </Group>

    <Group title="Параметры запуска" count={argv.length ? parseArgs(argv).length : undefined}>
      {argv.length > 0 && <>
        <pre className="launch-line"><code>{argv.map(shellWord).join(' ')}</code></pre>
        <LaunchTable rows={parseArgs(argv)} />
      </>}
      {launched && !argv.length && <p className="muted">{docker ? 'Команда не задана.'
        : 'Pod не переопределяет команду и аргументы: используются ENTRYPOINT и CMD образа.'}</p>}
      {launched && !docker && argv.length > 0 && !list(c.command).length && <p className="note">Команда (ENTRYPOINT) — из образа, показаны аргументы из Pod.</p>}
      {!launched && <p className="note">Снимок от агента старше 0.8: параметры запуска не собирались.</p>}
    </Group>

    <Group title="Ресурсы">
      <Fields rows={[
        ['CPU request', c.cpu_request != null ? `${c.cpu_request} ядра` : ''],
        ['CPU limit', c.cpu_limit != null ? `${c.cpu_limit} ядра` : '', 'не ограничен'],
        ['Привязка к CPU', text(c.cpuset)],
        ['Memory request', c.memory_request_bytes != null ? bytes(c.memory_request_bytes as number) : ''],
        ['Memory limit', c.memory_limit_bytes != null ? bytes(c.memory_limit_bytes as number) : '', 'не ограничен'],
        ['/dev/shm', c.shm_size_bytes != null ? bytes(c.shm_size_bytes as number) : ''],
        ['GPU', gpu.devices.length || gpu.limits.length ? <Chips items={[...gpu.devices, ...gpu.limits]} /> : ''],
      ]} />
    </Group>

    <Group title="Переменные окружения" count={env.length || undefined}>
      <EnvTable rows={env} />
      {collected && !env.length && <p className="muted">Переменные окружения не заданы.</p>}
      {!collected && <p className="note">Снимок от агента старой версии: показаны только переменные выбора GPU.</p>}
      {collected && !docker && env.length > 0 && <p className="note">Значения из Secret, ConfigMap и полей Pod в спецификации не хранятся — показан их источник.</p>}
    </Group>

    {files.map(([path, rows]) => <Group key={path} title={`Из файла ${path}`} count={rows.length || undefined}>
      <EnvTable rows={rows} />
      {!rows.length && <p className="muted">Файл есть, переменных в нем нет.</p>}
      <p className="note">Приложение читает этот файл само (например, godotenv), поэтому его переменных нет в окружении контейнера.
        Переменная из окружения контейнера обычно имеет приоритет над одноименной из файла.</p>
    </Group>)}

    <Group title="Порты и сети" count={ports.length || undefined}>
      {ports.length > 0 && <div className="table-wrap"><Table className="dense">
        <TableHeader><TableRow>
          {docker && <><TableHead>Адрес хоста</TableHead><TableHead>Порт хоста</TableHead></>}
          <TableHead>Порт контейнера</TableHead><TableHead>Протокол</TableHead>
        </TableRow></TableHeader>
        <TableBody>{ports.map((p, n) => <TableRow key={n}>
          {docker && <><TableCell><code>{p.hostIp || '*'}</code></TableCell><TableCell><code>{p.hostPort || '—'}</code></TableCell></>}
          <TableCell><code>{p.port}</code></TableCell><TableCell>{p.protocol || '—'}</TableCell>
        </TableRow>)}</TableBody>
      </Table></div>}
      {!ports.length && <p className="muted">Порты не опубликованы.</p>}
      <Fields rows={[
        ['Сети', networks.length ? <Chips items={networks} /> : ''],
        ['Network mode', host(c.network_mode)],
      ]} />
    </Group>

    <Group title="Тома и mounts" count={mounts.length || undefined}>
      {mounts.length > 0 ? <div className="table-wrap"><Table className="dense">
        <TableHeader><TableRow>
          {docker && <TableHead>Тип</TableHead>}<TableHead>{docker ? 'Источник' : 'Том'}</TableHead>
          <TableHead>Путь в контейнере</TableHead><TableHead>Доступ</TableHead>
        </TableRow></TableHeader>
        <TableBody>{mounts.map((m, n) => <TableRow key={n}>
          {docker && <TableCell>{m.type || '—'}</TableCell>}
          <TableCell><code className="break-all">{m.source || '—'}</code></TableCell>
          <TableCell><code className="break-all">{m.destination}</code></TableCell>
          <TableCell>{m.readonly ? <span className={badge('info')}>read-only</span> : 'чтение и запись'}</TableCell>
        </TableRow>)}</TableBody>
      </Table></div> : <p className="muted">Нет подключенных томов.</p>}
    </Group>

    <Group title="Запуск и проверки">
      <Fields rows={[
        ['Restart policy', text(c.restart_policy), 'не задана'],
        ['Проверки здоровья', probes.length ? <span className="flex flex-col gap-1">{probes.map(p => <code key={p}>{p}</code>)}</span> : '', 'нет'],
        ['Пользователь процесса', text(c.user) && <code>{text(c.user)}</code>, 'по умолчанию образа'],
        ['Драйвер логов', text(c.log_driver)],
      ]} />
    </Group>

    <Group title="Безопасность и изоляция">
      <Fields rows={[
        ['Privileged', yes(c.privileged, 'да')],
        ['Read-only rootfs', c.readonly_rootfs ? <span className={badge('')}>да</span> : <span className="muted">нет</span>],
        ['Добавленные capabilities', list(c.cap_add).length ? <Chips items={list(c.cap_add)} /> : ''],
        ['Удаленные capabilities', list(c.cap_drop).length ? <Chips items={list(c.cap_drop)} /> : ''],
        ['PID mode', host(c.pid_mode)],
        ['IPC mode', host(c.ipc_mode)],
      ]} />
      {ulimits.length > 0 && <div className="table-wrap"><Table className="dense">
        <TableHeader><TableRow><TableHead>Ulimit</TableHead><TableHead>Soft</TableHead><TableHead>Hard</TableHead></TableRow></TableHeader>
        <TableBody>{ulimits.map(u => <TableRow key={u.name}>
          <TableCell><code>{u.name}</code></TableCell><TableCell>{u.soft ?? '—'}</TableCell><TableCell>{u.hard ?? '—'}</TableCell>
        </TableRow>)}</TableBody>
      </Table></div>}
    </Group>

    {extra.length > 0 && <Group title="Прочее">
      <Fields rows={extra.map(([k, v]) => [k, <code className="break-all">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</code>])} />
    </Group>}
  </div>;
}
