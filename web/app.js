/* ============================================================
   乡村基层活动智能签到管理平台 —— 演示前端逻辑
   纯原生 JS，无构建、无外部依赖
   ============================================================ */
'use strict';

/* ---------------- 基础工具 ---------------- */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function toast(msg, type = 'info') {
  const wrap = $('#toast-wrap');
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

function fmtDT(s) {
  if (!s) return '—';
  return String(s).replace('T', ' ').slice(0, 16);
}

function pct(x) {
  return x === null || x === undefined ? '—' : (x * 100).toFixed(1) + '%';
}

function fmtArgs(a) {
  if (a === null || a === undefined) return '';
  try { return JSON.stringify(a); } catch (e) { return String(a); }
}

const EVENT_STATUS = {
  pending: ['badge-gray', '未开始'], active: ['badge-green', '进行中'],
  finished: ['badge-blue', '已结束'], cancelled: ['badge-red', '已取消'],
};
const CHECKIN_STATUS = {
  signed: ['badge-green', '已签到'], late: ['badge-orange', '迟到'],
  absent: ['badge-red', '缺勤'], leave: ['badge-blue', '请假'],
  abnormal: ['badge-purple', '异常'],
};
const LEAVE_STATUS = {
  pending: ['badge-gray', '待审批'], approved: ['badge-green', '已通过'],
  rejected: ['badge-red', '已驳回'], cancelled: ['badge-gray', '已撤销'],
};
const RELATION = {
  householder: '户主', spouse: '配偶', son: '儿子', daughter: '女儿',
  father: '父亲', mother: '母亲', other: '其他',
};
const GENDER = { male: '男', female: '女' };
const ROLE_LABEL = { admin: '管理员', staff: '工作人员', family: '家庭用户' };

function badge(map, key) {
  const [cls, label] = map[key] || ['badge-gray', key || '—'];
  return `<span class="badge ${cls}">${label}</span>`;
}

/* ---------------- 状态与请求 ---------------- */
let TOKEN = localStorage.getItem('am_token') || '';
let USER = null;
try { USER = JSON.parse(localStorage.getItem('am_user') || 'null'); } catch (e) { USER = null; }

const isRole = (...roles) => USER && roles.includes(USER.role);

async function api(method, path, body, isForm = false) {
  const headers = {};
  if (TOKEN) headers['Authorization'] = 'Bearer ' + TOKEN;
  if (body !== undefined && !isForm) headers['Content-Type'] = 'application/json';
  const resp = await fetch('/api' + path, {
    method,
    headers,
    body: body === undefined ? undefined : (isForm ? body : JSON.stringify(body)),
  });
  let json;
  try { json = await resp.json(); } catch (e) { json = { code: resp.status, message: '响应解析失败' }; }
  if (json.code === 401) { logout('登录已过期，请重新登录'); throw new Error(json.message); }
  return json;
}

function logout(msg) {
  TOKEN = '';
  USER = null;
  localStorage.removeItem('am_token');
  localStorage.removeItem('am_user');
  $('#login-view').classList.remove('hidden');
  $('#app-view').classList.add('hidden');
  if (msg) toast(msg, 'error');
}

/* ---------------- 弹窗 ---------------- */
let modalCallback = null;
function openModal(title, html, cb) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = html;
  $('#modal-overlay').classList.remove('hidden');
  modalCallback = cb || null;
}
function closeModal() {
  $('#modal-overlay').classList.add('hidden');
  modalCallback = null;
}
$('#modal-close').addEventListener('click', closeModal);
$('#modal-overlay').addEventListener('click', (e) => { if (e.target.id === 'modal-overlay') closeModal(); });

/* ---------------- 登录 / 注册 ---------------- */
$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const username = $('#login-username').value.trim();
  const password = $('#login-password').value;
  $('#login-error').classList.add('hidden');
  const btn = e.target.querySelector('button');
  btn.disabled = true;
  try {
    const json = await api('POST', '/auth/login', { username, password });
    if (json.code !== 0) throw new Error(json.message);
    TOKEN = json.data.access_token;
    USER = json.data.user;
    localStorage.setItem('am_token', TOKEN);
    localStorage.setItem('am_user', JSON.stringify(USER));
    enterApp();
    toast('登录成功，欢迎 ' + USER.real_name);
  } catch (err) {
    $('#login-error').textContent = err.message;
    $('#login-error').classList.remove('hidden');
  } finally { btn.disabled = false; }
});

$('#open-register').addEventListener('click', () => {
  openModal('注册家庭账号（户主）', `
    <form id="register-form" autocomplete="off">
      <label class="field"><span>账号（4~20 位字母数字）</span><input id="reg-username" required pattern="[A-Za-z0-9_]{4,20}"></label>
      <label class="field"><span>密码（至少 6 位）</span><input id="reg-password" type="password" required minlength="6"></label>
      <label class="field"><span>户主姓名</span><input id="reg-realname" required></label>
      <label class="field"><span>手机号（选填）</span><input id="reg-phone" placeholder="11 位手机号"></label>
      <button type="submit" class="btn btn-primary btn-block">注册并登录</button>
    </form>
    <p style="font-size:12px;color:#6b7280;margin-top:10px;">注册成功会自动创建家庭档案与户主成员，并直接登录。</p>
  `);
  $('#register-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = {
      username: $('#reg-username').value.trim(),
      password: $('#reg-password').value,
      real_name: $('#reg-realname').value.trim(),
    };
    const phone = $('#reg-phone').value.trim();
    if (phone) body.phone = phone;
    const btn = ev.target.querySelector('button');
    btn.disabled = true;
    try {
      const json = await api('POST', '/auth/register', body);
      if (json.code !== 0) throw new Error(json.message);
      toast('注册成功，正在登录…', 'success');
      closeModal();
      const login = await api('POST', '/auth/login', { username: body.username, password: body.password });
      TOKEN = login.data.access_token;
      USER = login.data.user;
      localStorage.setItem('am_token', TOKEN);
      localStorage.setItem('am_user', JSON.stringify(USER));
      enterApp();
      toast('欢迎 ' + USER.real_name, 'success');
    } catch (err) { toast('注册失败：' + err.message, 'error'); }
    finally { btn.disabled = false; }
  });
});

$('#logout-btn').addEventListener('click', () => { logout(); toast('已退出登录'); });

/* ---------------- 应用入口 ---------------- */
function enterApp() {
  $('#login-view').classList.add('hidden');
  $('#app-view').classList.remove('hidden');
  $('#user-chip').textContent = `${USER.real_name} · ${ROLE_LABEL[USER.role] || USER.role}`;
  showView('dashboard');
}

$$('.nav-item').forEach((btn) => {
  btn.addEventListener('click', () => showView(btn.dataset.view));
});

const CURRENT = { view: null, convId: null, page: {} };

function showView(name) {
  CURRENT.view = name;
  $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === name));
  const content = $('#content');
  content.innerHTML = '';
  if (name === 'dashboard') renderDashboard(content);
  else if (name === 'events') renderEvents(content);
  else if (name === 'leaves') renderLeaves(content);
  else if (name === 'chat') renderChat(content);
}

/* ============================================================
   总览
   ============================================================ */
async function renderDashboard(el) {
  if (isRole('admin', 'staff')) {
    el.innerHTML = `<div class="view-title">统计总览</div><div class="stat-grid" id="stat-grid"></div>
      <div class="card"><div class="card-title">家庭参与排行
        <span style="float:right;display:flex;gap:6px;">
          <button class="btn btn-ghost btn-sm" id="exp-events">导出活动统计 CSV</button>
          <button class="btn btn-ghost btn-sm" id="exp-families">导出家庭排行 CSV</button>
        </span></div><div id="rank-table"></div></div>`;
    try {
      const ov = await api('GET', '/statistics/overview');
      if (ov.code !== 0) throw new Error(ov.message);
      const d = ov.data;
      $('#stat-grid').innerHTML = [
        ['家庭数', d.total_families], ['成员数', d.total_members],
        ['活动总数', d.total_events], ['进行中', d.active_events],
        ['已结束', d.finished_events], ['签到次数', d.total_checkins],
        ['平均出勤率', pct(d.avg_attendance_rate)],
      ].map(([lbl, num]) => `<div class="stat-card"><div class="num">${esc(num)}</div><div class="lbl">${lbl}</div></div>`).join('');
    } catch (e) { $('#stat-grid').innerHTML = `<div class="empty">总览加载失败：${esc(e.message)}</div>`; }

    loadRanking();
    $('#exp-events').addEventListener('click', () => downloadCsv('type=events'));
    $('#exp-families').addEventListener('click', () => downloadCsv('type=families'));
  } else {
    el.innerHTML = `<div class="view-title">我的家庭</div><div id="my-family"></div>`;
    try {
      const json = await api('GET', '/families/me');
      if (json.code !== 0) throw new Error(json.message);
      const f = json.data;
      $('#my-family').innerHTML = `
        <div class="stat-grid">
          ${[['户号', f.household_no], ['村组', f.village || '—'], ['成员数', f.member_count], ['需签到', f.checkin_required_count]].map(
            ([lbl, num]) => `<div class="stat-card"><div class="num" style="font-size:17px;">${esc(num)}</div><div class="lbl">${lbl}</div></div>`).join('')}
        </div>
        <div class="card">
          <div class="card-title">家庭成员
            <button class="btn btn-primary btn-sm" style="float:right;" id="add-member">添加成员</button>
          </div>
          <table class="tbl"><thead><tr><th>姓名</th><th>关系</th><th>性别</th><th>手机号</th><th>需签到</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>${f.members.map((m) => `
            <tr>
              <td>${esc(m.name)}</td><td>${esc(RELATION[m.relation] || m.relation)}</td>
              <td>${esc(GENDER[m.gender] || '—')}</td><td>${esc(m.phone || '—')}</td>
              <td>${m.needs_checkin ? '是' : '否'}</td><td>${m.status === 'active' ? badge({ active: ['badge-green', '正常'] }, 'active') : badge({ inactive: ['badge-gray', '已停用'] }, 'inactive')}</td>
              <td><button class="btn btn-ghost btn-sm" data-face="${m.id}" data-name="${esc(m.name)}">录入人脸</button></td>
            </tr>`).join('')}</tbody></table>
        </div>`;
      $('#add-member').addEventListener('click', () => openAddMember());
      $$('#my-family [data-face]').forEach((b) => b.addEventListener('click', () => openFaceRegister(Number(b.dataset.face), b.dataset.name)));
    } catch (e) { $('#my-family').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
  }
}

async function loadRanking() {
  const box = $('#rank-table');
  try {
    const json = await api('GET', '/statistics/families?page_size=20');
    if (json.code !== 0) throw new Error(json.message);
    const items = json.data.items || [];
    if (!items.length) { box.innerHTML = '<div class="empty">暂无参与过已结束活动的家庭</div>'; return; }
    box.innerHTML = `<table class="tbl"><thead><tr><th>#</th><th>户号</th><th>户主</th><th>村组</th><th>在册成员</th><th>参与活动</th><th>签到次数</th><th>出勤率</th></tr></thead>
      <tbody>${items.map((r, i) => `<tr><td>${i + 1}</td><td>${esc(r.household_no || '—')}</td><td>${esc(r.owner_name)}</td>
        <td>${esc(r.village || '—')}</td><td>${r.active_member_count}</td><td>${r.event_participated_count}</td>
        <td>${r.total_checkins}</td><td>${pct(r.attendance_rate)}</td></tr>`).join('')}</tbody></table>`;
  } catch (e) { box.innerHTML = `<div class="empty">排行加载失败：${esc(e.message)}</div>`; }
}

async function downloadCsv(query) {
  try {
    const resp = await fetch('/api/statistics/export?' + query, {
      headers: TOKEN ? { Authorization: 'Bearer ' + TOKEN } : {},
    });
    if (!resp.ok) { const j = await resp.json().catch(() => null); throw new Error(j?.message || '导出失败'); }
    const blob = await resp.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = '签到统计_' + new Date().toISOString().slice(0, 10) + '.csv';
    a.click();
    URL.revokeObjectURL(a.href);
    toast('已开始下载', 'success');
  } catch (e) { toast(e.message, 'error'); }
}

/* ---------------- 成员 / 人脸（家庭视角） ---------------- */
function openAddMember() {
  openModal('添加家庭成员', `
    <form id="add-member-form">
      <div class="field-row">
        <label class="field"><span>姓名 *</span><input id="m-name" required></label>
        <label class="field"><span>关系</span><select id="m-relation">
          ${Object.entries(RELATION).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}
        </select></label>
      </div>
      <div class="field-row">
        <label class="field"><span>性别</span><select id="m-gender"><option value="">未知</option><option value="male">男</option><option value="female">女</option></select></label>
        <label class="field"><span>手机号</span><input id="m-phone" placeholder="选填"></label>
      </div>
      <label class="field"><span>身份证号（选填，自动识别性别/生日）</span><input id="m-idcard" placeholder="18 位身份证号"></label>
      <label class="field"><span>是否需要签到</span><select id="m-needs"><option value="true">是</option><option value="false">否</option></select></label>
      <button type="submit" class="btn btn-primary btn-block">添加</button>
    </form>`);
  $('#add-member-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = {
      name: $('#m-name').value.trim(),
      relation: $('#m-relation').value,
      gender: $('#m-gender').value || undefined,
      phone: $('#m-phone').value.trim() || undefined,
      id_card: $('#m-idcard').value.trim() || undefined,
      needs_checkin: $('#m-needs').value === 'true',
    };
    try {
      const json = await api('POST', '/members', body);
      if (json.code !== 0) throw new Error(json.message);
      closeModal(); toast('成员添加成功', 'success');
      showView('dashboard');
    } catch (e) { toast('添加失败：' + e.message, 'error'); }
  });
}

function openFaceRegister(memberId, memberName) {
  openModal('人脸录入：' + memberName, `
    <form id="face-form">
      <label class="field"><span>选择照片（单人正面、光线充足）</span><input id="face-file" type="file" accept="image/*" required></label>
      <button type="submit" class="btn btn-primary btn-block">提交录入（百度人脸检测）</button>
    </form>
    <p style="font-size:12px;color:#6b7280;margin-top:8px;">上传后会调用真实百度人脸识别 API 检测并入库。</p>`);
  $('#face-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const file = $('#face-file').files[0];
    const fd = new FormData();
    fd.append('member_id', String(memberId));
    fd.append('file', file);
    const btn = ev.target.querySelector('button');
    btn.disabled = true;
    try {
      const json = await api('POST', '/face/register', fd, true);
      if (json.code !== 0) throw new Error(json.message);
      closeModal(); toast(`人脸录入成功（检测到 ${json.data.detect.face_num} 张脸）`, 'success');
    } catch (e) { toast('录入失败：' + e.message, 'error'); }
    finally { btn.disabled = false; }
  });
}

/* ============================================================
   活动与签到
   ============================================================ */
async function renderEvents(el) {
  el.innerHTML = `<div class="view-title">签到活动
      ${isRole('admin') ? '<button class="btn btn-primary btn-sm" id="create-event">创建活动</button>' : ''}</div>
    <div class="card"><div id="event-list"></div></div>`;
  if (isRole('admin')) $('#create-event').addEventListener('click', openCreateEvent);
  await loadEvents();
}

async function loadEvents() {
  const box = $('#event-list');
  try {
    const json = await api('GET', '/events?page_size=50');
    if (json.code !== 0) throw new Error(json.message);
    const items = json.data.items || [];
    if (!items.length) { box.innerHTML = '<div class="empty">还没有活动，管理员可点击右上角创建</div>'; return; }
    box.innerHTML = `<table class="tbl"><thead><tr><th>活动名称</th><th>地点</th><th>开始时间</th><th>结束时间</th><th>迟到阈值</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>${items.map((ev) => `
        <tr>
          <td><strong>${esc(ev.name)}</strong></td><td>${esc(ev.location || '—')}</td>
          <td>${fmtDT(ev.start_time)}</td><td>${fmtDT(ev.end_time)}</td>
          <td>${ev.late_threshold_minutes} 分钟</td><td>${badge(EVENT_STATUS, ev.status)}</td>
          <td class="ops">
            <button class="btn btn-ghost btn-sm" data-detail="${ev.id}">详情</button>
            ${isRole('admin') ? `
              ${ev.status === 'pending' ? `<button class="btn btn-success btn-sm" data-status="${ev.id}" data-to="active">开始</button>
                <button class="btn btn-warn btn-sm" data-status="${ev.id}" data-to="cancelled">取消</button>` : ''}
              ${ev.status === 'active' ? `<button class="btn btn-success btn-sm" data-status="${ev.id}" data-to="finished">结束</button>` : ''}
              ${ev.status === 'pending' ? `<button class="btn btn-danger btn-sm" data-del="${ev.id}">删除</button>` : ''}
            ` : ''}
          </td>
        </tr>`).join('')}</tbody></table>`;
    $$('#event-list [data-detail]').forEach((b) => b.addEventListener('click', () => openEventDetail(Number(b.dataset.detail))));
    $$('#event-list [data-status]').forEach((b) => b.addEventListener('click', () => changeEventStatus(Number(b.dataset.status), b.dataset.to)));
    $$('#event-list [data-del]').forEach((b) => b.addEventListener('click', () => deleteEvent(Number(b.dataset.del))));
  } catch (e) { box.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

function openCreateEvent() {
  const now = new Date();
  const toLocal = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  const start = toLocal(now);
  const end = toLocal(new Date(now.getTime() + 2 * 3600 * 1000));
  openModal('创建签到活动（管理员）', `
    <form id="create-event-form">
      <label class="field"><span>活动名称 *</span><input id="e-name" required maxlength="100"></label>
      <label class="field"><span>活动地点</span><input id="e-location" maxlength="100"></label>
      <div class="field-row">
        <label class="field"><span>开始时间 *</span><input id="e-start" type="datetime-local" value="${start}" required></label>
        <label class="field"><span>结束时间 *</span><input id="e-end" type="datetime-local" value="${end}" required></label>
      </div>
      <div class="field-row">
        <label class="field"><span>迟到阈值（分钟）</span><input id="e-late" type="number" min="0" value="15"></label>
        <label class="field"><span>说明</span><input id="e-desc" placeholder="选填"></label>
      </div>
      <button type="submit" class="btn btn-primary btn-block">创建</button>
    </form>`);
  $('#create-event-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = {
      name: $('#e-name').value.trim(),
      location: $('#e-location').value.trim() || undefined,
      description: $('#e-desc').value.trim() || undefined,
      start_time: $('#e-start').value + ':00',
      end_time: $('#e-end').value + ':00',
      late_threshold_minutes: Number($('#e-late').value || 15),
    };
    try {
      const json = await api('POST', '/events', body);
      if (json.code !== 0) throw new Error(json.message);
      closeModal(); toast('活动创建成功', 'success');
      loadEvents();
    } catch (e) { toast('创建失败：' + e.message, 'error'); }
  });
}

async function changeEventStatus(id, to) {
  try {
    const json = await api('PUT', `/events/${id}/status`, { status: to });
    if (json.code !== 0) throw new Error(json.message);
    toast('操作成功', 'success');
    loadEvents();
  } catch (e) { toast('操作失败：' + e.message, 'error'); }
}

async function deleteEvent(id) {
  if (!confirm('确定删除该活动吗？（仅未开始且无签到记录的活动可删除）')) return;
  try {
    const json = await api('DELETE', `/events/${id}`);
    if (json.code !== 0) throw new Error(json.message);
    toast('已删除', 'success');
    loadEvents();
  } catch (e) { toast('删除失败：' + e.message, 'error'); }
}

async function openEventDetail(eventId) {
  openModal('活动详情', '<div class="empty">加载中…</div>');
  try {
    const json = await api('GET', `/checkins/events/${eventId}`);
    if (json.code !== 0) throw new Error(json.message);
    const d = json.data;
    const ev = d.event, s = d.summary;
    const ops = isRole('admin', 'staff') ? `
      <div style="display:flex;gap:8px;margin-bottom:14px;">
        <button class="btn btn-primary btn-sm" id="d-manual">手动签到</button>
        <button class="btn btn-primary btn-sm" id="d-face">人脸签到</button>
      </div>` : '';
    $('#modal-body').innerHTML = `
      <div class="stat-grid" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr));">
        ${[['应签到', s.total_expected], ['已签到', s.signed], ['迟到', s.late], ['缺勤', s.absent], ['请假', s.leave], ['出勤率', pct(s.attendance_rate)]].map(
          ([lbl, num]) => `<div class="stat-card"><div class="num" style="font-size:19px;">${esc(num)}</div><div class="lbl">${lbl}</div></div>`).join('')}
      </div>
      ${ops}
      <table class="tbl"><thead><tr><th>成员</th><th>状态</th><th>方式</th><th>签到时间</th><th>人脸分</th></tr></thead>
      <tbody>${(d.checkins.items || []).map((c) => `
        <tr><td>${esc(c.member_name)}</td><td>${badge(CHECKIN_STATUS, c.status)}</td>
          <td>${c.method ? (c.method === 'face' ? '人脸' : '手动') : '系统'}</td>
          <td>${fmtDT(c.checked_at)}</td><td>${c.face_score ?? '—'}</td></tr>`).join('')}</tbody></table>`;
    if (isRole('admin', 'staff')) {
      $('#d-manual').addEventListener('click', () => openManualCheckin(eventId, ev.name));
      $('#d-face').addEventListener('click', () => openFaceCheckin(eventId, ev.name));
    }
  } catch (e) {
    $('#modal-body').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

async function openManualCheckin(eventId, eventName) {
  openModal(`手动签到：${eventName}`, `
    <form id="manual-form">
      <label class="field"><span>家庭</span><select id="mc-family" required><option value="">加载中…</option></select></label>
      <label class="field"><span>成员</span><select id="mc-member" required><option value="">请先选择家庭</option></select></label>
      <label class="field"><span>备注（选填）</span><input id="mc-remark"></label>
      <button type="submit" class="btn btn-primary btn-block">提交签到</button>
    </form>`);
  const famSel = $('#mc-family');
  try {
    const j = await api('GET', '/families?page_size=100');
    if (j.code !== 0) throw new Error(j.message);
    famSel.innerHTML = '<option value="">请选择家庭</option>' + (j.data.items || []).map(
      (f) => `<option value="${f.id}">${esc(f.household_no)} · ${esc(f.owner_name)}</option>`).join('');
  } catch (e) { famSel.innerHTML = `<option value="">${esc(e.message)}</option>`; }
  famSel.addEventListener('change', async () => {
    const fid = famSel.value;
    const memSel = $('#mc-member');
    memSel.innerHTML = '<option value="">加载中…</option>';
    if (!fid) { memSel.innerHTML = '<option value="">请先选择家庭</option>'; return; }
    try {
      const j = await api('GET', `/members?family_id=${fid}&page_size=100`);
      if (j.code !== 0) throw new Error(j.message);
      memSel.innerHTML = '<option value="">请选择成员</option>' + (j.data.items || []).filter((m) => m.status === 'active')
        .map((m) => `<option value="${m.id}">${esc(m.name)}${m.needs_checkin ? '' : '（无需签到）'}</option>`).join('');
    } catch (e) { memSel.innerHTML = `<option value="">${esc(e.message)}</option>`; }
  });
  $('#manual-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try {
      const json = await api('POST', '/checkins/manual', {
        event_id: eventId,
        member_id: Number($('#mc-member').value),
        remark: $('#mc-remark').value.trim() || undefined,
      });
      if (json.code !== 0) throw new Error(json.message);
      closeModal(); toast('签到成功', 'success');
      openEventDetail(eventId);
    } catch (e) { toast('签到失败：' + e.message, 'error'); }
  });
}

function openFaceCheckin(eventId, eventName) {
  openModal(`人脸签到：${eventName}`, `
    <form id="face-checkin-form">
      <label class="field"><span>现场照片（人脸识别签到）</span><input id="fc-file" type="file" accept="image/*" required></label>
      <button type="submit" class="btn btn-primary btn-block">提交识别签到</button>
    </form>
    <p style="font-size:12px;color:#6b7280;margin-top:8px;">调用百度人脸库 1:N 搜索，识别成功且匹配本人后自动落签到记录。</p>`);
  $('#face-checkin-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const fd = new FormData();
    fd.append('event_id', String(eventId));
    fd.append('file', $('#fc-file').files[0]);
    const btn = ev.target.querySelector('button');
    btn.disabled = true;
    try {
      const json = await api('POST', '/checkins/face', fd, true);
      if (json.code !== 0) throw new Error(json.message);
      closeModal(); toast(`签到成功：${json.data.member_name}`, 'success');
      openEventDetail(eventId);
    } catch (e) { toast('签到失败：' + e.message, 'error'); }
    finally { btn.disabled = false; }
  });
}

/* ============================================================
   请假管理
   ============================================================ */
async function renderLeaves(el) {
  el.innerHTML = `<div class="view-title">请假管理
      ${isRole('family', 'admin') ? '<button class="btn btn-primary btn-sm" id="add-leave">提交请假</button>' : ''}</div>
    <div class="card"><div id="leave-list"></div></div>`;
  if (isRole('family', 'admin')) $('#add-leave').addEventListener('click', openAddLeave);
  await loadLeaves();
}

async function loadLeaves() {
  const box = $('#leave-list');
  try {
    const json = await api('GET', '/leaves?page_size=50');
    if (json.code !== 0) throw new Error(json.message);
    const items = json.data.items || [];
    if (!items.length) { box.innerHTML = '<div class="empty">暂无请假记录</div>'; return; }
    box.innerHTML = `<table class="tbl"><thead><tr><th>成员</th><th>活动</th><th>原因</th><th>状态</th><th>提交时间</th><th>审批备注</th><th>操作</th></tr></thead>
      <tbody>${items.map((lv) => `
        <tr>
          <td>${esc(lv.member_name)}</td><td>${esc(lv.event_name)}</td><td>${esc(lv.reason)}</td>
          <td>${badge(LEAVE_STATUS, lv.status)}</td><td>${fmtDT(lv.created_at)}</td>
          <td>${esc(lv.review_remark || '—')}</td>
          <td class="ops">${isRole('admin') && lv.status === 'pending' ? `
            <button class="btn btn-success btn-sm" data-review="${lv.id}" data-to="approved">通过</button>
            <button class="btn btn-danger btn-sm" data-review="${lv.id}" data-to="rejected">驳回</button>` : '—'}</td>
        </tr>`).join('')}</tbody></table>`;
    $$('#leave-list [data-review]').forEach((b) => b.addEventListener('click', () => reviewLeave(Number(b.dataset.review), b.dataset.to)));
  } catch (e) { box.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

async function reviewLeave(leaveId, to) {
  const remark = prompt(to === 'approved' ? '通过备注（选填）' : '驳回原因（选填）');
  if (remark === null) return;
  try {
    const json = await api('PUT', `/leaves/${leaveId}/approve`, {
      status: to, remark: remark.trim() || undefined,
    });
    if (json.code !== 0) throw new Error(json.message);
    toast('已' + (to === 'approved' ? '通过' : '驳回'), 'success');
    loadLeaves();
  } catch (e) { toast('操作失败：' + e.message, 'error'); }
}

async function openAddLeave() {
  let famSelect = '';
  if (isRole('admin')) {
    famSelect = `<label class="field"><span>家庭</span><select id="lv-family"><option value="">请选择家庭</option></select></label>`;
  }
  openModal('提交请假申请', `
    <form id="leave-form">
      ${famSelect}
      <label class="field"><span>活动</span><select id="lv-event" required><option value="">加载中…</option></select></label>
      <label class="field"><span>成员</span><select id="lv-member" required><option value="">加载中…</option></select></label>
      <label class="field"><span>请假原因 *</span><textarea id="lv-reason" rows="2" required maxlength="255"></textarea></label>
      <button type="submit" class="btn btn-primary btn-block">提交</button>
    </form>
    <p style="font-size:12px;color:#6b7280;margin-top:8px;">需提前申请，由管理员审批；已结束/已取消的活动不可申请。</p>`);
  try {
    const ev = await api('GET', '/events?page_size=100');
    if (ev.code !== 0) throw new Error(ev.message);
    $('#lv-event').innerHTML = '<option value="">请选择活动</option>' + (ev.data.items || [])
      .filter((e) => ['pending', 'active'].includes(e.status))
      .map((e) => `<option value="${e.id}">${esc(e.name)}（${fmtDT(e.start_time)}）</option>`).join('');
  } catch (e) { $('#lv-event').innerHTML = `<option value="">${esc(e.message)}</option>`; }

  const loadMembers = async () => {
    const memSel = $('#lv-member');
    memSel.innerHTML = '<option value="">加载中…</option>';
    try {
      let items;
      if (isRole('admin')) {
        const fid = $('#lv-family').value;
        if (!fid) { memSel.innerHTML = '<option value="">请先选择家庭</option>'; return; }
        const j = await api('GET', `/members?family_id=${fid}&page_size=100`);
        items = j.data.items || [];
      } else {
        const j = await api('GET', '/families/me');
        items = (j.data.members || []).filter((m) => m.status === 'active');
      }
      memSel.innerHTML = '<option value="">请选择成员</option>' + items.map(
        (m) => `<option value="${m.id}">${esc(m.name)}</option>`).join('');
    } catch (e) { memSel.innerHTML = `<option value="">${esc(e.message)}</option>`; }
  };

  if (isRole('admin')) {
    try {
      const fam = await api('GET', '/families?page_size=100');
      if (fam.code === 0) {
        $('#lv-family').innerHTML = '<option value="">请选择家庭</option>' + (fam.data.items || [])
          .map((f) => `<option value="${f.id}">${esc(f.household_no)} · ${esc(f.owner_name)}</option>`).join('');
        $('#lv-family').addEventListener('change', loadMembers);
      }
    } catch (e) { /* 忽略 */ }
  }
  loadMembers();

  $('#leave-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = {
      event_id: Number($('#lv-event').value),
      member_id: Number($('#lv-member').value),
      reason: $('#lv-reason').value.trim(),
    };
    try {
      const json = await api('POST', '/leaves', body);
      if (json.code !== 0) throw new Error(json.message);
      closeModal(); toast('请假申请已提交', 'success');
      loadLeaves();
    } catch (e) { toast('提交失败：' + e.message, 'error'); }
  });
}

/* ============================================================
   AI 智能助手（SSE 流式）
   ============================================================ */
async function renderChat(el) {
  CURRENT.convId = null;
  el.innerHTML = `
    <div class="chat-layout">
      <div class="chat-side">
        <div class="chat-side-head"><button class="btn btn-primary btn-block btn-sm" id="new-conv">＋ 新对话</button></div>
        <div class="chat-side-list" id="conv-list"><div class="empty">加载中…</div></div>
      </div>
      <div class="chat-main">
        <div class="chat-head">AI 智能助手<span style="font-weight:400;color:#6b7280;font-size:12px;margin-left:8px;">回答基于平台真实数据，支持工具调用</span></div>
        <div class="chat-msgs" id="chat-msgs"></div>
        <div class="chat-hint" id="chat-hint">试试问：${isRole('family') ? '"我家这次活动的签到情况怎么样"' : '"给我一份统计总览" 或 "生成活动简报"'}</div>
        <div class="chat-input-bar">
          <input id="chat-input" placeholder="输入你的问题，Enter 发送" autocomplete="off">
          <button class="btn btn-primary" id="chat-send">发送</button>
        </div>
      </div>
    </div>`;
  $('#new-conv').addEventListener('click', () => {
    CURRENT.convId = null;
    $('#chat-msgs').innerHTML = '';
    refreshConvList();
  });
  const sendBtn = $('#chat-send');
  const input = $('#chat-input');
  const doSend = () => { const v = input.value.trim(); if (v) { chatSend(v); input.value = ''; } };
  sendBtn.addEventListener('click', doSend);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSend(); });
  refreshConvList();
}

async function refreshConvList() {
  const box = $('#conv-list');
  if (!box) return;
  try {
    const json = await api('GET', '/assistant/conversations?page_size=30');
    if (json.code !== 0) throw new Error(json.message);
    const items = json.data.items || [];
    box.innerHTML = items.length
      ? items.map((c) => `<button class="chat-conv-item ${c.id === CURRENT.convId ? 'active' : ''}" data-conv="${c.id}">${esc(c.title || '新对话')}</button>`).join('')
      : '<div class="empty">还没有对话，开始问吧</div>';
    $$('#conv-list [data-conv]').forEach((b) => b.addEventListener('click', () => loadConversation(Number(b.dataset.conv))));
  } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

async function loadConversation(convId) {
  CURRENT.convId = convId;
  $('#chat-msgs').innerHTML = '';
  refreshConvList();
  try {
    const json = await api('GET', `/assistant/conversations/${convId}/messages?page_size=50`);
    if (json.code !== 0) throw new Error(json.message);
    (json.data.items || []).forEach((m) => {
      if (m.role === 'user') appendMsg('user', m.content);
      else if (m.role === 'assistant') {
        if (m.tool_calls && Array.isArray(m.tool_calls)) {
          m.tool_calls.forEach((t) => appendTool(t));
        }
        if (m.content) appendMsg('bot', m.content);
      }
    });
    scrollChat();
  } catch (e) { appendMsg('bot', '加载失败：' + e.message); }
}

function appendMsg(role, text) {
  const box = $('#chat-msgs');
  if (!box) return;
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = text;
  box.appendChild(div);
  return div;
}

function appendTool(t) {
  const box = $('#chat-msgs');
  if (!box) return;
  const div = document.createElement('div');
  div.className = 'tool-chip';
  div.innerHTML = `<div class="t-name">🔧 调用工具：${esc(t.name)}</div><div class="t-detail">参数 ${esc(fmtArgs(t.args))}<br>结果 ${esc(t.result || '')}</div>`;
  box.appendChild(div);
}

function scrollChat() {
  const box = $('#chat-msgs');
  if (box) box.scrollTop = box.scrollHeight;
}

let chatBusy = false;
async function chatSend(text) {
  if (chatBusy) return;
  chatBusy = true;
  $('#chat-send').disabled = true;
  appendMsg('user', text);
  const replyEl = appendMsg('bot', '');
  const typing = () => { replyEl.innerHTML = '<span class="typing">思考中…</span>'; };
  typing();
  scrollChat();
  try {
    const resp = await fetch('/api/assistant/chat/stream', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + TOKEN,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ message: text, conversation_id: CURRENT.convId || null }),
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    if (!resp.body) throw new Error('无响应流');

    replyEl.textContent = '';
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let started = false;
    let finalReply = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, idx).replace(/\r$/, '').trim();
        buf = buf.slice(idx + 1);
        if (!line.startsWith('data:')) continue;
        let payload;
        try { payload = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
        if (payload.type === 'tool') { appendTool(payload); }
        else if (payload.type === 'token') {
          started = true;
          replyEl.textContent += payload.content;
          finalReply += payload.content;
          scrollChat();
        }
        else if (payload.type === 'done') {
          CURRENT.convId = payload.conversation_id || CURRENT.convId;
          refreshConvList();
          if (!started && !finalReply) replyEl.textContent = payload.reply || '';
        }
      }
    }
    if (!replyEl.textContent) replyEl.textContent = '（未收到回复）';
  } catch (e) {
    // 流式失败降级：走非流式接口
    try {
      const json = await api('POST', '/assistant/chat', { message: text, conversation_id: CURRENT.convId || null });
      if (json.code !== 0) throw new Error(json.message);
      (json.data.tool_calls || []).forEach((t) => appendTool(t));
      replyEl.textContent = json.data.reply;
      CURRENT.convId = json.data.conversation_id;
      refreshConvList();
    } catch (e2) {
      replyEl.textContent = '请求失败：' + e2.message;
    }
  } finally {
    chatBusy = false;
    $('#chat-send').disabled = false;
    scrollChat();
  }
}

/* ---------------- 启动 ---------------- */
(function init() {
  if (USER && TOKEN) { enterApp(); } else { $('#login-view').classList.remove('hidden'); }
})();
