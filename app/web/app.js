const $ = (id) => document.getElementById(id);
const CHINA_TIME_ZONE = 'Asia/Shanghai';
const CHINA_TIME_OFFSET = '+08:00';
const chinaDateTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: CHINA_TIME_ZONE,
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});
let proxies = [];
let proxyStats = [];
let proxySorts = [];
let serviceDefinitions = [];
let editingServiceUrl = null;
let toastTimer;

function parseTimestamp(value) {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value !== 'string' || !value.trim()) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatChinaTime(value) {
  const date = parseTimestamp(value);
  return date
    ? `${chinaDateTimeFormatter.format(date).replaceAll('/', '-')} ${CHINA_TIME_OFFSET}`
    : (value || '');
}

function notify(message, error = false) {
  const toast = $('toast');
  toast.textContent = message;
  toast.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.className = 'toast', 3600);
}

function values() {
  return {
    controller: $('controller').value.trim(),
    secret: $('secret').value,
    test_url: $('test-url').value.trim(),
    rounds: Number($('rounds').value),
    interval: Number($('interval').value),
    concurrency: Number($('concurrency').value),
    timeout_ms: Number($('timeout-ms').value),
    request_timeout: Number($('request-timeout').value),
    services: serviceDefinitions,
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: 'no-store',
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (_error) { data = {}; }
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function formData() {
  return values();
}

function setServiceFieldError(fieldId, errorId, message = '') {
  const field = $(fieldId);
  const error = $(errorId);
  error.textContent = message;
  field.setAttribute('aria-invalid', message ? 'true' : 'false');
  if (message) field.setAttribute('aria-describedby', errorId);
  else field.removeAttribute('aria-describedby');
}

function clearServiceErrors() {
  setServiceFieldError('service-name', 'service-name-error');
  setServiceFieldError('service-url', 'service-url-error');
}

function showServiceSaveError(message) {
  if (/url|地址|http/i.test(message)) {
    setServiceFieldError('service-url', 'service-url-error', message);
  } else {
    setServiceFieldError('service-name', 'service-name-error', message);
  }
}

function openServiceDialog(service = null) {
  editingServiceUrl = service?.url || null;
  $('service-add-title').textContent = service ? '修改探测目标' : '添加探测目标';
  $('service-add-dialog').querySelector('.subhead').textContent = service
    ? '修改用于可达性探测的 HTTP 地址'
    : '添加一个用于可达性探测的 HTTP 地址';
  $('submit-service-add').textContent = service ? '保存修改' : '添加目标';
  $('service-add-form').reset();
  if (service) {
    $('service-name').value = service.name;
    $('service-url').value = service.url;
  }
  clearServiceErrors();
  $('service-add-dialog').showModal();
  $('service-name').focus();
}

function closeServiceDialog() {
  editingServiceUrl = null;
  $('service-add-dialog').close();
}

async function saveConfig(showMessage = true) {
  const data = await api('/api/config', { method: 'POST', body: JSON.stringify(formData()) });
  $('secret').value = '';
  $('secret').placeholder = data.config.secret_configured ? '密钥已保存，留空保持不变' : '未设置';
  if (showMessage) notify('配置已保存');
  return data.config;
}

async function loadConfig() {
  const config = await api('/api/config');
  $('controller').value = config.controller;
  $('test-url').value = config.test_url;
  $('rounds').value = config.rounds;
  $('interval').value = config.interval;
  $('concurrency').value = config.concurrency;
  $('timeout-ms').value = config.timeout_ms;
  $('request-timeout').value = config.request_timeout;
  $('secret').placeholder = config.secret_configured ? '密钥已保存，留空保持不变' : '未设置';
  serviceDefinitions = config.services || [];
  renderServiceCards();
}

function updateSortHeaders() {
  const directionLabels = { asc: '正序', desc: '倒序' };
  const indicators = { asc: '↑', desc: '↓', none: '↕' };
  document.querySelectorAll('.sort-button').forEach((button) => {
    const order = proxySorts.findIndex((sort) => sort.key === button.dataset.sortKey);
    const current = order >= 0 ? proxySorts[order].direction : 'none';
    button.setAttribute('aria-sort', current === 'asc' ? 'ascending' : current === 'desc' ? 'descending' : 'none');
    button.setAttribute('aria-label', `按${button.dataset.sortLabel}排序，当前${directionLabels[current] || '未排序'}`);
    button.querySelector('.sort-order').textContent = order >= 0 ? order + 1 : '';
    button.querySelector('.sort-indicator').textContent = indicators[current];
  });
}

function renderActiveSorts() {
  const container = $('active-sorts');
  container.replaceChildren();
  container.classList.toggle('empty', proxySorts.length === 0);
  if (!proxySorts.length) return;
  const directionLabels = { asc: '正序', desc: '倒序' };
  const caption = document.createElement('span');
  caption.textContent = '当前排序：';
  container.append(caption);
  proxySorts.forEach((sort, index) => {
    const chip = document.createElement('span');
    chip.className = 'sort-chip';
    chip.append(document.createTextNode(`${index + 1}. ${sort.label} · ${directionLabels[sort.direction]}`));
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'sort-remove';
    remove.textContent = 'x';
    remove.setAttribute('aria-label', `移除第${index + 1}级${sort.label}排序`);
    remove.addEventListener('click', () => removeSort(sort.key));
    chip.append(remove);
    container.append(chip);
  });
}

function cycleSort(metric) {
  const index = proxySorts.findIndex((sort) => sort.key === metric);
  if (index < 0) {
    proxySorts.push({ key: metric, label: document.querySelector(`[data-sort-key="${metric}"]`).dataset.sortLabel, direction: 'asc' });
  } else {
    proxySorts[index].direction = proxySorts[index].direction === 'asc' ? 'desc' : 'asc';
  }
  renderProxies(proxyStats);
}

function removeSort(metric) {
  proxySorts = proxySorts.filter((sort) => sort.key !== metric);
  renderProxies(proxyStats);
}

function renderProxies(rows = []) {
  const body = $('proxy-rows');
  proxyStats = rows;
  updateSortHeaders();
  renderActiveSorts();
  body.replaceChildren();
  if (!proxies.length) {
    renderTableMessage(body, '暂无可探测代理，请检查控制器连接或策略组设置');
    return;
  }
  const stats = new Map(rows.map((row) => [row.proxy, row]));
  const orderedProxies = proxies.map((proxy, index) => ({ proxy, index }));
  if (proxySorts.length) {
    orderedProxies.sort((left, right) => {
      for (const { key, direction } of proxySorts) {
        const leftValue = stats.get(left.proxy.name)?.[key];
        const rightValue = stats.get(right.proxy.name)?.[key];
        const leftNumber = Number(leftValue);
        const rightNumber = Number(rightValue);
        const leftMissing = leftValue === null || leftValue === undefined || !Number.isFinite(leftNumber);
        const rightMissing = rightValue === null || rightValue === undefined || !Number.isFinite(rightNumber);
        if (leftMissing || rightMissing) {
          if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
          continue;
        }
        const difference = leftNumber - rightNumber;
        if (difference) return direction === 'asc' ? difference : -difference;
      }
      return left.index - right.index;
    });
  }
  const query = $('proxy-search').value.trim().toLocaleLowerCase();
  const visibleProxies = query
    ? orderedProxies.filter(({ proxy }) => proxy.name.toLocaleLowerCase().includes(query))
    : orderedProxies;
  if (!visibleProxies.length) {
    renderTableMessage(body, '没有找到匹配的代理节点');
    return;
  }
  visibleProxies.forEach(({ proxy }) => {
    const stat = stats.get(proxy.name);
    const tr = document.createElement('tr');
    const nameCell = document.createElement('td');
    const name = document.createElement('div'); name.className = 'proxy-name'; name.textContent = proxy.name; nameCell.append(name);
    const typeCell = document.createElement('td'); typeCell.className = 'proxy-type wide-only'; typeCell.textContent = proxy.type;
    const cells = [
      stat ? `${stat.total}/${$('rounds').value || 0}` : '—',
      stat ? `${(stat.rate * 100).toFixed(0)}%` : '—',
      stat && stat.minimum !== null ? `${stat.minimum.toFixed(1)} ms` : '—',
      stat && stat.maximum !== null ? `${stat.maximum.toFixed(1)} ms` : '—',
      stat && stat.average !== null ? `${stat.average.toFixed(1)} ms` : '—',
      stat && stat.jitter !== null ? `${stat.jitter.toFixed(1)} ms` : '—',
    ];
    tr.append(nameCell, typeCell);
    cells.forEach((value) => { const td = document.createElement('td'); td.className = 'number'; td.textContent = value; tr.append(td); });
    body.append(tr);
  });
}

function renderTableMessage(body, message) {
  const row = document.createElement('tr');
  const cell = document.createElement('td');
  cell.colSpan = 8;
  cell.className = 'empty';
  cell.textContent = message;
  row.append(cell);
  body.append(row);
}

function formatBytes(value) {
  let size = Number(value);
  if (!Number.isFinite(size)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unit = 0;
  while (Math.abs(size) >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function configureVisibleRows() {
  const settings = [
    { id: 'collection-log-limit', key: 'collectionLogRows', target: 'log-body', rowHeight: 20.4, padding: 14 },
    { id: 'controller-log-limit', key: 'controllerLogRows', target: 'controller-log-body', rowHeight: 20.4, padding: 14 },
  ];
  settings.forEach(({ id, key, target, rowHeight, padding }) => {
    const select = $(id);
    const saved = localStorage.getItem(key);
    if (saved && [...select.options].some((option) => option.value === saved)) select.value = saved;
    const apply = () => {
      localStorage.setItem(key, select.value);
      $(target).style.height = `${Math.ceil(Number(select.value) * rowHeight + padding)}px`;
    };
    select.addEventListener('change', apply);
    apply();
  });

  const connectionSelect = $('connection-limit');
  const saved = localStorage.getItem('connectionRows');
  if (saved && [...connectionSelect.options].some((option) => option.value === saved)) connectionSelect.value = saved;
  connectionSelect.addEventListener('change', () => {
    localStorage.setItem('connectionRows', connectionSelect.value);
    resizeConnectionTable();
  });
  resizeConnectionTable();
}

function connectionAddress(metadata, prefix) {
  const ip = metadata[`${prefix}IP`] || '';
  const port = metadata[`${prefix}Port`];
  const host = prefix === 'destination' ? metadata.host : '';
  const address = host || ip || '—';
  return port ? `${address}:${port}` : address;
}

function resizeConnectionTable() {
  const wrap = $('connection-table-wrap');
  const header = wrap.querySelector('thead').getBoundingClientRect().height;
  const row = wrap.querySelector('tbody tr')?.getBoundingClientRect().height || 37;
  wrap.style.height = `${Math.ceil(header + Number($('connection-limit').value) * row)}px`;
}

function renderConnections(state) {
  const connections = Array.isArray(state.connections) ? state.connections : [];
  const body = $('connection-rows');
  $('connection-count').textContent = `${connections.length} 条连接`;
  body.replaceChildren();
  if (!connections.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td'); cell.colSpan = 8; cell.className = 'empty';
    cell.textContent = '当前没有活动连接'; row.append(cell); body.append(row); resizeConnectionTable(); return;
  }
  connections.forEach((connection) => {
    const metadata = connection.metadata || {};
    const row = document.createElement('tr');
    const values = [
      metadata.network || metadata.type || '—',
      metadata.process || '—',
      formatChinaTime(connection.start) || '—',
      connectionAddress(metadata, 'source'),
      connectionAddress(metadata, 'destination'),
      Array.isArray(connection.chains) ? connection.chains.join(' → ') : '—',
      formatBytes(connection.upload), formatBytes(connection.download),
    ];
    values.forEach((value, index) => {
      const cell = document.createElement('td');
      if (index > 5) cell.className = 'number';
      cell.textContent = String(value); row.append(cell);
    });
    body.append(row);
  });
  resizeConnectionTable();
}

function replaceLogContents(body, children) {
  const scrollTop = body.scrollTop;
  const wasAtBottom = body.scrollHeight - body.clientHeight - scrollTop <= 2;
  body.replaceChildren(...children);
  body.scrollTop = wasAtBottom ? body.scrollHeight : scrollTop;
}

function renderControllerLogs(entries) {
  const body = $('controller-log-body');
  const logs = Array.isArray(entries) ? entries : [];
  $('controller-log-count').textContent = `${logs.length} 条`;
  if (!logs.length) {
    const empty = document.createElement('div'); empty.className = 'log-line'; empty.textContent = '等待控制器运行日志…';
    replaceLogContents(body, [empty]);
    return;
  }
  const lines = logs.map((entry) => {
    const line = document.createElement('div'); line.className = 'log-line controller-log';
    const time = document.createElement('time'); time.textContent = formatChinaTime(entry.time);
    const level = document.createElement('span'); level.className = 'controller-log-level'; level.textContent = entry.level || '';
    const message = document.createElement('span');
    const fields = entry.fields && typeof entry.fields === 'object' ? ` ${JSON.stringify(entry.fields)}` : '';
    message.textContent = `${entry.message || ''}${fields}`;
    line.append(time, level, message);
    return line;
  });
  replaceLogContents(body, lines);
}

function paintMonitor(state) {
  const traffic = state.traffic || {};
  const totals = state.connection_totals || {};
  $('traffic-up').textContent = `${formatBytes(traffic.up)}/s`;
  $('traffic-down').textContent = `${formatBytes(traffic.down)}/s`;
  $('traffic-up-total').textContent = formatBytes(traffic.upTotal ?? totals.uploadTotal);
  $('traffic-down-total').textContent = formatBytes(traffic.downTotal ?? totals.downloadTotal);
  const split = state.connection_traffic || {};
  for (const kind of ['proxy', 'direct']) {
    const values = split[kind] || {};
    $(`${kind}-up-rate`).textContent = `${formatBytes(values.up)}/s`;
    $(`${kind}-down-rate`).textContent = `${formatBytes(values.down)}/s`;
    $(`${kind}-up-total`).textContent = formatBytes(values.upTotal);
    $(`${kind}-down-total`).textContent = formatBytes(values.downTotal);
  }
  renderConnections(state);
  renderControllerLogs(state.controller_logs);
  const errors = state.errors || {};
  const labels = { traffic: '/traffic', logs: '/logs', connections: '/connections' };
  $('endpoint-errors').textContent = Object.entries(errors).map(([key, error]) => `${labels[key] || key}: ${error}`).join('；');
  $('monitor-status').textContent = Object.keys(errors).length ? '部分接口不可用' : '控制器已连接';
}

async function refreshProxies() {
  await saveConfig(false);
  $('refresh').disabled = true;
  try {
    const data = await api('/api/proxies');
    proxies = data.proxies;
    renderProxies();
    notify(`已加载 ${proxies.length} 个代理`);
  } finally {
    $('refresh').disabled = false;
  }
}

const exitIpProviderLabels = { ipify: 'ipify', ipapi: 'ipapi.co', ipwho: 'ipwho.is', ipwhois: 'ipwhois.app' };
const exitIpFields = ['ip', 'country', 'city', 'timezone', 'utc_offset', 'meta'];

function setExitIpRow(provider, values) {
  exitIpFields.forEach((field) => {
    $(`exit-ip-${provider}-${field}`).textContent = values[field] || '—';
  });
}

async function refreshExitIp(provider, showError = false) {
  const row = document.querySelector(`[data-provider-row="${provider}"]`);
  const button = row.querySelector('.exit-ip-provider');
  const status = $('exit-ip-status');
  const proxyMode = document.querySelector('input[name="exit-ip-proxy-mode"]:checked').value;
  setExitIpRow(provider, Object.fromEntries(exitIpFields.map((field) => [field, '探测中…'])));
  const networkLabel = proxyMode === 'direct' ? '直连' : 'Python 默认网络配置';
  status.textContent = `通过${networkLabel}探测 ${exitIpProviderLabels[provider] || provider}`;
  button.disabled = true;
  try {
    const query = new URLSearchParams({ provider, proxy_mode: proxyMode });
    const data = await api(`/api/exit-ip?${query}`);
    if (data.ip) {
      const checkedAt = formatChinaTime(data.checked_at) || '刚刚';
      setExitIpRow(provider, { ...data, meta: `IPv${data.version} · ${checkedAt}` });
      status.textContent = `IPv${data.version} · ${checkedAt}`;
    } else {
      setExitIpRow(provider, {});
      status.textContent = data.error || '探测失败';
      if (showError) notify(status.textContent, true);
    }
  } catch (error) {
    setExitIpRow(provider, {});
    status.textContent = error.message;
    if (showError) notify(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderServiceCards() {
  const list = $('service-status-list');
  list.replaceChildren(...serviceDefinitions.map((service) => {
    const card = document.createElement('div');
    card.className = 'service-status-item';
    card.dataset.serviceUrl = service.url;
    card.dataset.status = 'idle';
    card.setAttribute('role', 'listitem');
    card.innerHTML = `<div class="service-status-name"></div><div class="service-status-detail service-status-url"></div><div class="service-status-detail service-status-result">未探测</div><div class="service-status-detail" data-field="meta">—</div><div class="service-status-actions"><button class="button service-edit" type="button">修改</button><button class="button service-check-one" type="button">探测</button><button class="button service-remove" type="button">删除</button></div>`;
    card.querySelector('.service-status-name').textContent = service.name;
    card.querySelector('.service-status-url').textContent = service.url;
    card.querySelector('.service-edit').addEventListener('click', () => editService(service.url));
    card.querySelector('.service-check-one').addEventListener('click', () => checkServices([service.url]));
    card.querySelector('.service-remove').addEventListener('click', () => removeService(service.url));
    return card;
  }));
}

function editService(url) {
  const service = serviceDefinitions.find((item) => item.url === url);
  if (service) openServiceDialog(service);
}

function setServiceCard(url, result) {
  const card = [...document.querySelectorAll('[data-service-url]')].find((item) => item.dataset.serviceUrl === url);
  if (!card) return;
  const statusText = { available: '可达', timeout: '超时', failed: '请求失败' };
  card.dataset.status = result.status;
  card.querySelector('.service-status-result').textContent = result.status_code
    ? `${statusText[result.status] || result.status} · HTTP ${result.status_code} · ${result.delay_ms} ms`
    : `${statusText[result.status] || result.status}${result.error ? ` · ${result.error}` : ''}`;
  card.querySelector('[data-field="meta"]').textContent = result.checked_at
    ? formatChinaTime(result.checked_at)
    : '—';
}

async function checkServices(urls = null) {
  const cards = urls
    ? [...document.querySelectorAll('[data-service-url]')].filter((card) => urls.includes(card.dataset.serviceUrl))
    : [...document.querySelectorAll('[data-service-url]')];
  const buttons = cards.map((card) => card.querySelector('.service-check-one'));
  buttons.forEach((button) => { button.disabled = true; });
  buttons.forEach((button) => {
    const card = button.closest('[data-service-url]');
    card.dataset.status = 'checking';
    card.querySelector('.service-status-result').textContent = '探测中…';
  });
  $('service-check-all').disabled = true;
  try {
    const query = new URLSearchParams({
      proxy_mode: document.querySelector('input[name="service-proxy-mode"]:checked').value,
    });
    urls?.forEach((url) => query.append('url', url));
    const data = await api(`/api/services?${query}`);
    data.services.forEach((result) => setServiceCard(result.url, result));
  } catch (error) {
    buttons.forEach((button) => {
      const card = button.closest('[data-service-url]');
      card.dataset.status = 'failed';
      card.querySelector('.service-status-result').textContent = error.message;
    });
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
    $('service-check-all').disabled = false;
  }
}

async function removeService(url) {
  serviceDefinitions = serviceDefinitions.filter((service) => service.url !== url);
  if (!serviceDefinitions.length) {
    notify('至少保留一个可达性探测目标', true);
    await loadConfig();
    return;
  }
  try {
    await saveConfig();
    renderServiceCards();
  } catch (error) {
    notify(error.message, true);
    await loadConfig();
  }
}

async function saveService() {
  const name = $('service-name').value.trim();
  const url = $('service-url').value.trim();
  const originalUrl = editingServiceUrl;
  clearServiceErrors();
  if (!name) {
    setServiceFieldError('service-name', 'service-name-error', '请输入目标名称');
    $('service-name').focus();
    return;
  }
  if (!/^https?:\/\/[^\s/]+(?:\/[^\s]*)?$/i.test(url)) {
    setServiceFieldError('service-url', 'service-url-error', '目标 URL 必须以 http:// 或 https:// 开头');
    $('service-url').focus();
    return;
  }
  const duplicatedName = serviceDefinitions.some((service) => (
    service.url !== originalUrl && service.name.toLocaleLowerCase() === name.toLocaleLowerCase()
  ));
  const duplicatedUrl = serviceDefinitions.some((service) => service.url !== originalUrl && service.url === url);
  if (duplicatedName || duplicatedUrl) {
    if (duplicatedName) setServiceFieldError('service-name', 'service-name-error', '目标名称已存在');
    if (duplicatedUrl) setServiceFieldError('service-url', 'service-url-error', '目标 URL 已存在');
    (duplicatedName ? $('service-name') : $('service-url')).focus();
    return;
  }
  const submit = $('submit-service-add');
  submit.disabled = true;
  const previousDefinitions = serviceDefinitions;
  serviceDefinitions = originalUrl
    ? serviceDefinitions.map((service) => service.url === originalUrl ? { name, url } : service)
    : [...serviceDefinitions, { name, url }];
  try {
    await saveConfig();
    $('service-add-form').reset();
    renderServiceCards();
    closeServiceDialog();
  } catch (error) {
    serviceDefinitions = previousDefinitions;
    showServiceSaveError(error.message);
  } finally {
    submit.disabled = false;
  }
}

async function startRun() {
  const proxyNames = proxies.map((proxy) => proxy.name);
  if (!proxyNames.length) { notify('没有可探测的代理节点', true); return; }
  $('run').disabled = true;
  try {
    await api('/api/run', { method: 'POST', body: JSON.stringify({ ...formData(), proxies: proxyNames }) });
    $('secret').value = '';
    $('secret').placeholder = '密钥已保存，留空保持不变';
    notify('延迟采样已开始');
    await refreshState();
  } catch (error) {
    notify(error.message, true);
    $('run').disabled = false;
  }
}

async function stopRun() {
  try { await api('/api/stop', { method: 'POST', body: '{}' }); }
  catch (error) { notify(error.message, true); }
}

function paintState(state) {
  paintMonitor(state);
  $('metric-status').textContent = state.running ? (state.stopping ? '停止中' : '采样中') : state.status;
  $('run').disabled = state.running;
  $('stop').disabled = !state.running || state.stopping;
  $('metric-proxies').textContent = Object.keys(state.proxy_types || {}).length;
  const samples = state.rows.reduce((total, row) => total + row.total, 0);
  const successes = state.rows.reduce((total, row) => total + row.success, 0);
  $('metric-samples').textContent = `${samples}/${state.total_samples}`;
  $('metric-rate').textContent = samples ? `${(successes / samples * 100).toFixed(0)}%` : '—';
  if (state.proxy_types && Object.keys(state.proxy_types).length) {
    proxies = Object.entries(state.proxy_types).map(([name, type]) => ({ name, type }));
    renderProxies(state.rows);
  }
  if (state.logs.length) {
    const logLines = state.logs.map((line) => {
      const div = document.createElement('div'); div.className = 'log-line'; div.textContent = line;
      return div;
    });
    replaceLogContents($('log-body'), logLines);
  }
}

async function refreshState() {
  try { paintState(await api('/api/state')); }
  catch (error) { $('metric-status').textContent = '无法连接本机观测服务'; }
}

$('open-settings').addEventListener('click', () => $('settings-dialog').showModal());
$('close-settings').addEventListener('click', () => $('settings-dialog').close());
$('open-service-add').addEventListener('click', () => openServiceDialog());
$('close-service-add').addEventListener('click', closeServiceDialog);
$('cancel-service-add').addEventListener('click', closeServiceDialog);
$('save').addEventListener('click', async () => {
  try {
    await saveConfig();
    $('settings-dialog').close();
  } catch (error) {
    notify(error.message, true);
  }
});
$('refresh').addEventListener('click', () => refreshProxies().catch((error) => notify(error.message, true)));
document.querySelectorAll('.exit-ip-provider').forEach((button) => {
  button.addEventListener('click', () => refreshExitIp(button.dataset.provider, true));
});
$('service-check-all').addEventListener('click', () => checkServices());
$('service-name').addEventListener('input', () => setServiceFieldError('service-name', 'service-name-error'));
$('service-url').addEventListener('input', () => setServiceFieldError('service-url', 'service-url-error'));
$('service-add-form').addEventListener('submit', (event) => {
  event.preventDefault();
  saveService();
});
$('run').addEventListener('click', startRun);
$('stop').addEventListener('click', stopRun);
$('proxy-search').addEventListener('input', () => renderProxies(proxyStats));
document.querySelectorAll('.sort-button').forEach((button) => {
  button.addEventListener('click', () => cycleSort(button.dataset.sortKey));
});
(async () => {
  configureVisibleRows();
  try {
    await loadConfig();
    await refreshState();
    await refreshProxies();
  } catch (error) {
    notify(error.message, true);
  }
  setInterval(refreshState, 1000);
})();
