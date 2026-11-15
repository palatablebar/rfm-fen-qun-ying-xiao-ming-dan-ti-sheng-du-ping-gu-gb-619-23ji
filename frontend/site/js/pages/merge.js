import { api } from '../api.js';
import { ENUMS, can, mergeJobTone } from '../config.js';
import {
  badge,
  drawer,
  errorBox,
  errorMessage,
  esc,
  fmtNum,
  fmtPct,
  fmtTime,
  kv,
  statStrip,
  table,
  toast,
} from '../ui.js';

const state = { status: '', match_kind: '', jobId: '' };

function progressOf(job) {
  // 处理进度：以游标为辅助，主要靠 processed_profiles / profiles_before
  const total = Number(job.profiles_before || 0);
  const done = Number(job.processed_profiles || 0);
  if (!total) return 0;
  return Math.min(100, Math.round((done / total) * 100));
}

async function openMaster(view, ctx, memberId) {
  let detail;
  try {
    detail = await api.get(`/api/merge/masters/${memberId}`);
  } catch (err) {
    toast(errorMessage(err), 'err');
    return;
  }
  const m = detail.master || {};
  const profiles = detail.profiles || [];
  const merged = detail.merged_members || [];
  const coupons = detail.coupons || [];
  const { root, close } = drawer({
    title: `主档 ${esc(m.member_no || '')}`,
    subtitle: `${esc(m.name || '')} · ${esc(m.phone || '')} · 吸收 ${fmtNum(merged.length)} 份被合并档案`,
    body: `
      ${kv([
        ['主档会员号', esc(m.member_no)],
        ['姓名 / 城市', `${esc(m.name)} · ${esc(m.city)}`],
        ['手机号', esc(m.phone)],
        ['卡等级', esc(ENUMS.tier[m.tier] || m.tier)],
        ['合并订单数', fmtNum(detail.orders_count)],
        ['合并券响应', `${fmtNum(detail.coupons_count)} 条（核销 ${fmtNum(coupons.filter((c) => c.status === 'redeemed').length)}）`],
        ['历史券响应率', fmtPct(m.purchase_rate)],
        ['当前分群', esc(ENUMS.segment[m.segment_code] || m.segment_code || '未评分')],
      ])}
      <h3 style="margin:14px 0 8px;font-size:0.95rem">渠道档案（主档 + 被合并，共 ${profiles.length} 份）</h3>
      <div class="table-wrap"><table class="data" style="min-width:0">
        <thead><tr><th>状态</th><th>渠道</th><th>渠道会员号</th><th>姓名</th><th>手机号</th><th>外部账号</th><th>来源更新时间</th></tr></thead>
        <tbody>${profiles
          .map(
            (p) => `<tr>
              <td>${badge(p.status === 'active' ? 'ok' : 'warn', ENUMS.profileStatus[p.status] || p.status)}</td>
              <td>${esc(ENUMS.profileChannel[p.channel] || p.channel)}</td>
              <td class="mono">${esc(p.channel_member_no)}</td>
              <td>${esc(p.name)}</td>
              <td class="mono">${esc(p.phone || '—')}</td>
              <td class="mono muted">${p.external_id ? `${esc(p.external_type)}:${esc(p.external_id)}` : '—'}</td>
              <td class="mono">${fmtTime(p.source_updated_at)}</td>
            </tr>`,
          )
          .join('')}</tbody></table></div>

      <h3 style="margin:14px 0 8px;font-size:0.95rem">被合并会员（${merged.length}）</h3>
      ${
        merged.length
          ? `<div class="table-wrap"><table class="data" style="min-width:0">
            <thead><tr><th>会员号</th><th>姓名</th><th>手机号</th><th>城市</th></tr></thead>
            <tbody>${merged
              .map(
                (x) => `<tr><td class="mono">${esc(x.member_no)}</td><td>${esc(x.name)}</td>
                  <td class="mono">${esc(x.phone)}</td><td>${esc(x.city)}</td></tr>`,
              )
              .join('')}</tbody></table></div>`
          : '<p class="muted">无被合并档案。</p>'
      }

      <h3 style="margin:14px 0 8px;font-size:0.95rem">归并依据（${(detail.merge_records || []).length} 条）</h3>
      ${(detail.merge_records || [])
        .map(
          (r) => `<div class="panel" style="box-shadow:none;margin-bottom:8px">
            <div class="panel-body" style="padding:10px 12px">
              <div>${badge('info', r.match_kind_label)} <span class="mono muted">${esc(r.master_reason)}</span></div>
              <div class="note" style="margin-top:6px">移动订单 ${fmtNum(r.orders_moved)} 笔 · 移动券响应 ${fmtNum(r.coupons_moved)} 条 · 冲突字段 ${fmtNum(r.conflict_count)} 个</div>
            </div>
          </div>`,
        )
        .join('') || '<p class="muted">暂无归并记录。</p>'}

      <h3 style="margin:14px 0 8px;font-size:0.95rem">合并后订单（最近 ${detail.orders.length} 笔）</h3>
      ${
        detail.orders.length
          ? `<div class="table-wrap"><table class="data" style="min-width:0">
            <thead><tr><th>渠道</th><th>金额</th><th>用券</th><th>时间</th></tr></thead>
            <tbody>${detail.orders
              .map(
                (o) => `<tr><td>${esc(o.channel_label)}</td><td class="num">¥${Number(o.amount).toFixed(2)}</td>
                  <td>${o.coupon_used ? '是' : '—'}</td><td class="mono">${fmtTime(o.paid_at)}</td></tr>`,
              )
              .join('')}</tbody></table></div>`
          : '<p class="muted">暂无订单。</p>'
      }`,
    footer: `<span class="mono muted">订单与券响应已全部并入主档，被合并档案保留可追溯，不物理删除</span>`,
  });
  root.querySelectorAll('[data-close]').forEach((node) => node.addEventListener('click', close));
}

function conflictBlock(conflicts) {
  if (!conflicts || !conflicts.length) return '<span class="muted">无冲突</span>';
  return conflicts
    .map((c) => {
      const src = (c.sources || [])
        .map((s) => `${ENUMS.profileChannel[s.channel] || s.channel}「${esc(String(s.value))}」@${fmtTime(s.source_updated_at)}`)
        .join(' / ');
      return `<div style="margin-bottom:4px">
        <b>${esc(c.field_label || c.field)}</b> → 取 <b>${esc(String(c.chosen?.value ?? '—'))}</b>
        <div class="mono muted" style="font-size:0.78rem">来源依据：${src}</div>
      </div>`;
    })
    .join('');
}

async function openRecord(view, ctx, record) {
  const { root, close } = drawer({
    title: `归并记录 #${record.id}`,
    subtitle: `${record.match_kind_label} · 任务 #${record.merge_job_id}`,
    body: `${kv([
      ['识别依据', badge('info', record.match_kind_label)],
      ['匹配值', `<span class="mono">${esc(record.match_value || '—')}</span>${record.external_type ? ` <span class="muted">(${esc(record.external_type)})</span>` : ''}`],
      ['主档', `${esc(record.master?.member_no)} · ${esc(record.master?.name)}（${esc(ENUMS.profileChannel[record.master_profile?.channel] || '')}）`],
      ['被合并档案', `${esc(record.merged?.member_no)} · ${esc(record.merged?.name)}（${esc(ENUMS.profileChannel[record.merged_profile?.channel] || '')}）`],
      ['主档归属原因', `<span class="mono muted">${esc(record.master_reason)}</span>`],
      ['移动订单 / 券响应', `${fmtNum(record.orders_moved)} 笔 / ${fmtNum(record.coupons_moved)} 条`],
    ])}
    <h3 style="margin:14px 0 8px;font-size:0.95rem">冲突资料的来源与时间依据</h3>
    <div class="panel" style="box-shadow:none"><div class="panel-body">${conflictBlock(record.conflicts)}</div></div>`,
    footer: record.master?.id
      ? `<button class="btn accent" data-open-master>查看主档详情</button>`
      : '',
  });
  root.querySelector('[data-open-master]')?.addEventListener('click', () => {
    close();
    openMaster(view, ctx, record.master.id);
  });
  root.querySelectorAll('[data-close]').forEach((node) => node.addEventListener('click', close));
}

export const mergePage = {
  async render(view, ctx) {
    const canRun = can(ctx.user.role, 'merge:run');
    view.innerHTML = '<div class="empty"><div class="mark">正在核对跨渠道档案…</div></div>';
    let overview, jobs, records;
    try {
      const [ov, jb, rc] = await Promise.all([
        api.get('/api/merge/overview'),
        api.get('/api/merge/jobs', { status: state.status, limit: 20 }),
        api.get('/api/merge/records', {
          job_id: state.jobId,
          match_kind: state.match_kind,
          limit: 30,
        }),
      ]);
      overview = ov;
      jobs = jb;
      records = rc;
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => mergePage.render(view, ctx));
      return;
    }

    const removed = overview.profiles_total - overview.members_master;
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>跨渠道会员归并</h2>
          <span class="meta">按手机号 + 外部账号识别同一人 · 归并订单与历史券响应</span>
        </div>
        <div class="panel-body" style="display:grid;gap:12px">
          ${statStrip([
            { label: '归并前档案数', value: fmtNum(overview.before_count), hint: '门店+小程序+外卖各一份' },
            { label: '归并后主档数', value: fmtNum(overview.after_count), hint: `去重后真实人数` },
            { label: '被合并档案', value: fmtNum(overview.members_merged), hint: `减少 ${fmtNum(removed)} 份重复` },
            { label: '历史券响应', value: fmtNum(overview.coupon_total), hint: `核销 ${fmtNum(overview.coupon_redeemed)} 条` },
          ])}
          <div class="filter-bar">
            <div class="field"><label>归并任务</label>
              <select id="job-filter">
                <option value="">全部记录</option>
                ${jobs.items.map((j) => `<option value="${j.id}" ${String(j.id) === String(state.jobId) ? 'selected' : ''}>${esc(j.job_no)}（${esc(j.status_label)}）</option>`).join('')}
              </select>
            </div>
            <div class="field"><label>识别依据</label>
              <select id="kind-filter">
                <option value="">全部依据</option>
                <option value="phone" ${state.match_kind === 'phone' ? 'selected' : ''}>手机号一致</option>
                <option value="external" ${state.match_kind === 'external' ? 'selected' : ''}>外部账号一致</option>
              </select>
            </div>
            <div class="spacer"></div>
            ${
              canRun
                ? '<button class="btn accent" id="start-merge" data-testid="start-merge">发起归并</button>'
                : '<span class="note">只读角色可查看归并结果，不能发起任务</span>'
            }
            <button class="btn ghost" id="download-merge">下载归并结果 CSV</button>
          </div>
          <div class="note">
            任务从「待处理 → 处理中 → 已完成」；失败后可从已处理位置（断点游标）续跑，重试不新增重复档案。
            已确认名单仍按生成时的会员快照导出，待确认名单改用归并后的主档，避免重复触达。
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><h2>归并任务</h2><span class="meta">${fmtNum(jobs.total)} 个 · 含归并前后人数</span></div>
        ${table({
          rows: jobs.items,
          empty: { title: '还没有归并任务', hint: '点「发起归并」开始识别跨渠道同一人。' },
          columns: [
            { key: 'job_no', label: '任务号', className: 'mono' },
            { key: 'status', label: '状态', render: (r) => badge(mergeJobTone(r.status), r.status_label) },
            {
              key: 'profiles_before',
              label: '档案 前→后',
              className: 'num',
              render: (r) => `${fmtNum(r.profiles_before)} → <b>${fmtNum(r.profiles_after || '—')}</b>`,
            },
            {
              key: 'members_before',
              label: '主档 前→后',
              className: 'num',
              render: (r) => `${fmtNum(r.members_before)} → ${fmtNum(r.members_after || '—')}`,
            },
            { key: 'groups_merged', label: '归并组', className: 'num', render: (r) => fmtNum(r.groups_merged) },
            { key: 'records_merged', label: '被合并档案', className: 'num', render: (r) => fmtNum(r.records_merged) },
            { key: 'conflict_count', label: '冲突字段', className: 'num', render: (r) => fmtNum(r.conflict_count) },
            {
              key: 'progress',
              label: '进度 / 续跑',
              render: (r) =>
                r.status === 'failed'
                  ? `<button class="btn sm accent" data-retry="${r.id}">从断点续跑</button>`
                  : `<span class="mono muted">${progressOf(r)}% · 游标 ${fmtNum(r.last_cursor)}</span>`,
            },
          ],
        })}
      </section>

      <section class="panel">
        <div class="panel-head"><h2>归并记录（主档 / 被合并档案 / 冲突依据）</h2><span class="meta">${fmtNum(records.total)} 条 · 点击查看冲突来源与时间</span></div>
        ${table({
          rows: records.items,
          empty: { title: '暂无归并记录', hint: '发起归并后这里会列出每个被合并档案及其依据。' },
          columns: [
            { key: 'match_kind', label: '识别依据', render: (r) => badge('info', r.match_kind_label) },
            {
              key: 'master',
              label: '主档',
              render: (r) => `<b>${esc(r.master?.member_no)}</b><div class="mono muted">${esc(r.master?.name)} · ${esc(ENUMS.profileChannel[r.master_profile?.channel] || '')}</div>`,
            },
            {
              key: 'merged',
              label: '被合并档案',
              render: (r) => `${esc(r.merged?.member_no)}<div class="mono muted">${esc(r.merged?.name)} · ${esc(ENUMS.profileChannel[r.merged_profile?.channel] || '')}</div>`,
            },
            { key: 'orders_moved', label: '移动订单', className: 'num', render: (r) => fmtNum(r.orders_moved) },
            { key: 'coupons_moved', label: '移动券响应', className: 'num', render: (r) => fmtNum(r.coupons_moved) },
            {
              key: 'conflicts',
              label: '冲突',
              render: (r) => `${fmtNum((r.conflicts || []).length)} 个`,
            },
            { key: 'created_at', label: '时间', render: (r) => fmtTime(r.created_at) },
          ],
        })}
      </section>`;

    view.querySelector('#job-filter')?.addEventListener('change', (e) => {
      state.jobId = e.target.value;
      mergePage.render(view, ctx);
    });
    view.querySelector('#kind-filter')?.addEventListener('change', (e) => {
      state.match_kind = e.target.value;
      mergePage.render(view, ctx);
    });

    view.querySelector('#start-merge')?.addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = '归并处理中…';
      try {
        const job = await api.post('/api/merge/jobs', {});
        toast(`任务 ${job.job_no} 已完成：${job.profiles_before} → ${job.profiles_after} 个主档`);
        await mergePage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
        btn.disabled = false;
        btn.textContent = '发起归并';
      }
    });

    view.querySelector('#download-merge')?.addEventListener('click', async () => {
      try {
        const qs = state.jobId ? `?job_id=${state.jobId}` : '';
        await api.download(`/api/exports/merge-records.csv${qs}`, 'merge_records.csv');
        toast('已开始下载归并结果，审计日志同步写入');
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });

    view.querySelectorAll('[data-retry]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        btn.textContent = '续跑中…';
        try {
          const job = await api.post(`/api/merge/jobs/${btn.dataset.retry}/retry`, {});
          toast(`任务 ${job.job_no} 已续跑完成：${job.status_label}`);
          await mergePage.render(view, ctx);
        } catch (err) {
          toast(errorMessage(err), 'err');
          btn.disabled = false;
          btn.textContent = '从断点续跑';
        }
      });
    });

    view.querySelectorAll('table.data tbody tr').forEach((tr) => {
      tr.addEventListener('click', () => {
        const item = records.items.find((r) => String(r.id) === tr.dataset.id);
        if (item) openRecord(view, ctx, item);
      });
    });
  },
};

export default mergePage;
