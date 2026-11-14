import { api } from '../api.js';
import { ENUMS, audienceTone, can, liftTone } from '../config.js';
import {
  badge,
  budgetBar,
  drawer,
  errorBox,
  errorMessage,
  esc,
  field,
  fmtLift,
  fmtMoney,
  fmtNum,
  fmtPct,
  kv,
  statStrip,
  table,
  toast,
} from '../ui.js';

const state = { campaignId: '', budget: '', allocation: null, loading: false };

async function loadAllocation(view, ctx, campaigns) {
  const campaign = campaigns.find((item) => String(item.id) === String(state.campaignId)) || campaigns[0];
  if (!campaign) return null;
  const budget = Number(state.budget || campaign.budget);
  try {
    const data = await api.get(`/api/campaigns/${campaign.id}/allocation`, { budget });
    state.campaignId = String(campaign.id);
    return data;
  } catch (err) {
    toast(errorMessage(err), 'err');
    return null;
  }
}

function allocationTable(allocation) {
  return `<div class="table-wrap"><table class="data allocation-table">
    <thead><tr>
      <th>优先级</th><th>分群</th><th>lift</th><th>券面额</th><th>人均成本</th><th>lift/成本</th>
      <th>试算人数</th><th>券预算</th><th>预计增量</th><th>增量 GMV</th><th>ROI</th><th>状态</th>
    </tr></thead>
    <tbody>
      ${allocation.segments
        .map(
          (row) => `<tr data-code="${esc(row.segment_code)}">
            <td class="num">${row.priority ?? '—'}</td>
            <td><b>${esc(row.segment_name)}</b>
              <div class="mini">${esc(row.status === 'allocated' ? row.reason : row.reason)}</div>
            </td>
            <td class="num"><span class="lift-pill ${liftTone(row.lift)}">${fmtLift(row.lift)}</span></td>
            <td class="num">${fmtMoney(row.coupon_face, 0)}</td>
            <td class="num">${fmtMoney(row.unit_cost)}</td>
            <td class="num">${Number(row.efficiency).toFixed(2)}</td>
            <td class="num">${fmtNum(row.allocated)}<div class="mini">核心 ${fmtNum(row.core_allocated)} / 溢出 ${fmtNum(row.tail_allocated)}</div></td>
            <td class="num">${fmtMoney(row.budget)}</td>
            <td class="num">${fmtNum(row.inc_orders)}</td>
            <td class="num">${fmtMoney(row.inc_gmv, 0)}</td>
            <td class="num">${Number(row.roi).toFixed(2)}</td>
            <td>${badge(row.status === 'allocated' ? 'ok' : row.status === 'deferred' ? 'warn' : '', ENUMS.allocationStatus[row.status])}</td>
          </tr>`,
        )
        .join('')}
    </tbody>
  </table>
  <div class="note" style="margin-top:8px">试算为预算规划口径；点「生成名单」后按优先级在共享占用台账抢占冷却期外的真实会员，
    实际占用人数 / 预算 / 导出明细以台账为准，活动间不重复占人、不超预算。</div>
  </div>`;
}

function historyTable(audiences) {
  return table({
    rows: audiences,
    columns: [
      { key: 'audience_no', label: '名单号', className: 'mono' },
      { key: 'campaign_name', label: '活动' },
      {
        key: 'status',
        label: '状态',
        render: (row) => `${badge(audienceTone(row.status), row.status_label)}
          <div class="mini">${esc(ENUMS.lockPhase[row.lock_phase] || row.lock_phase || '预留占用')}${row.attempt > 1 ? ` · 第 ${fmtNum(row.attempt)} 轮` : ''}</div>`,
      },
      {
        key: 'member_count',
        label: '人数',
        className: 'num',
        render: (row) => `${fmtNum(row.member_count)}${row.locked_members != null && row.locked_members !== row.member_count
          ? `<div class="mini" style="color:var(--danger,#c0392b)">占用 ${fmtNum(row.locked_members)}</div>`
          : ''}`,
      },
      { key: 'budget', label: '预算', className: 'num', render: (row) => fmtMoney(row.budget) },
      { key: 'spend', label: '实花', className: 'num', render: (row) => fmtMoney(row.spend) },
      { key: 'inc_orders', label: '预计增量', className: 'num', render: (row) => fmtNum(row.inc_orders) },
      { key: 'roi', label: 'ROI', className: 'num', render: (row) => Number(row.roi).toFixed(2) },
      { key: 'operator_email', label: '生成人', render: (row) => `<span class="mono muted">${esc(row.operator_email)}</span>` },
    ],
    empty: { title: '还没有名单', hint: '选好活动与预算，点「生成名单」。' },
  });
}

function openAudience(view, ctx, audience, campaigns) {
  const rows = audience.allocation || [];
  const canConfirm = can(ctx.user.role, 'audience:confirm');
  const canGenerate = can(ctx.user.role, 'audience:generate');
  const isPending = audience.status === 'pending';
  const { root, close } = drawer({
    title: `名单 ${audience.audience_no}`,
    subtitle: `${audience.campaign_name || ''} · ${audience.status_label} · ${esc(ENUMS.lockPhase[audience.lock_phase] || audience.lock_phase || '预留占用')}`,
    body: `${kv([
      ['活动', esc(audience.campaign_name || '—')],
      ['名单人数', `${fmtNum(audience.member_count)} 人（占用台账 ${fmtNum(audience.locked_members ?? audience.member_count)} 人，逐行一致）`],
      ['预算 / 实花', `${fmtMoney(audience.budget)} / ${fmtMoney(audience.spend)}（不超预算）`],
      ['预计增量单量', fmtNum(audience.inc_orders)],
      ['增量 GMV', fmtMoney(audience.inc_gmv)],
      ['整体 ROI', `<b>${Number(audience.roi).toFixed(2)}</b>`],
      ['大盘购买率', fmtPct(audience.global_rate)],
      ['占用轮次', `第 ${fmtNum(audience.attempt || 1)} 轮（同活动重试不新增名单、不重复占人）`],
      ['生成人 / 时间', `${esc(audience.operator_email)} · ${esc((audience.created_at || '').slice(0, 16).replace('T', ' '))}`],
      ['确认人', esc(audience.confirmed_by || '—')],
    ])}
    <h3 style="margin:14px 0 6px;font-size:0.95rem">分群预算分配（实际占用口径）</h3>
    <div class="table-wrap"><table class="data" style="min-width:0">
      <thead><tr><th>分群</th><th>lift</th><th>实际占用</th><th>被挡/差额</th><th>预算</th><th>增量</th><th>ROI</th></tr></thead>
      <tbody>${rows
        .filter((row) => row.status === 'allocated')
        .map(
          (row) => `<tr><td>${esc(row.segment_name)}</td><td class="mono">${fmtLift(row.lift)}</td>
            <td class="num">${fmtNum(row.occupied_members ?? row.allocated)}</td>
            <td class="num">${fmtNum(row.blocked_members ?? 0)}</td>
            <td class="num">${fmtMoney(row.budget)}</td>
            <td class="num">${fmtNum(row.inc_orders)}</td><td class="num">${Number(row.roi).toFixed(2)}</td></tr>`,
        )
        .join('')}</tbody></table></div>
    <div class="note" style="margin-top:10px">${esc(audience.note || '')}</div>`,
    footer: canConfirm || canGenerate
      ? `<div class="rule-chipline">
          ${canConfirm && isPending ? '<button class="btn accent" data-confirm="confirmed">确认名单（占用转正）</button>' : ''}
          ${canConfirm && isPending ? '<button class="btn ghost" data-confirm="pushed">标记已推送券平台</button>' : ''}
          ${canGenerate && isPending ? '<button class="btn ghost" data-resume>续跑补人</button>' : ''}
          <button class="btn sm ghost" data-download>导出名单明细</button>
        </div>`
      : '<span class="mono muted">只读角色可以查看名单，但不能确认或导出</span>',
  });

  root.querySelectorAll('[data-confirm]').forEach((node) => {
    node.addEventListener('click', async () => {
      try {
        await api.patch(`/api/audiences/${audience.id}/review`, { status: node.dataset.confirm });
        toast('名单状态已更新，占用已一次性转为正式名单');
        close();
        await audiencesPage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
  });
  root.querySelector('[data-resume]')?.addEventListener('click', async () => {
    try {
      const updated = await api.post(`/api/audiences/${audience.id}/resume`, {});
      toast(`续跑完成：${updated.audience_no} 实际占用 ${fmtNum(updated.member_count)} 人`);
      close();
      await audiencesPage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
  root.querySelector('[data-download]')?.addEventListener('click', async () => {
    try {
      await api.download(`/api/exports/audiences.csv?audience_id=${audience.id}`, `${audience.audience_no}.csv`);
      toast('已开始下载名单明细（与占用台账一致）');
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
  void campaigns;
}

export const audiencesPage = {
  async render(view, ctx) {
    let campaigns;
    let history;
    try {
      [campaigns, history] = await Promise.all([
        api.get('/api/campaigns'),
        api.get('/api/audiences', { limit: 20 }),
      ]);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => audiencesPage.render(view, ctx));
      return;
    }
    if (!campaigns.items.length) {
      view.innerHTML = errorBox('还没有活动，请先执行 seed 或由管理员创建活动。');
      return;
    }
    if (!state.campaignId) state.campaignId = String(campaigns.items[0].id);
    const allocation = await loadAllocation(view, ctx, campaigns.items);
    const campaign = campaigns.items.find((item) => String(item.id) === state.campaignId) || campaigns.items[0];
    const admin = can(ctx.user.role, 'audience:generate');
    const totals = allocation ? allocation.totals : null;

    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>名单与提升度</h2>
          <span class="meta">${campaign.name} · ${campaign.status_label} · 券口径 ${campaign.channel}</span>
        </div>
        <div class="panel-body" style="display:grid;gap:12px">
          <div class="filter-bar">
            ${field({
              label: '活动',
              name: 'campaign_id',
              value: state.campaignId,
              options: campaigns.items.map((item) => ({
                value: item.id,
                label: `${item.name}（预算 ${fmtMoney(item.budget, 0)}）`,
              })),
            })}
            ${field({
              label: '本次预算（元）',
              name: 'budget',
              type: 'number',
              value: state.budget || campaign.budget,
              attrs: 'step="100" min="1"',
            })}
            <button class="btn ghost" data-testid="preview" id="preview">重新试算</button>
            ${admin ? '<button class="btn accent" id="generate" data-testid="generate">生成名单</button>' : '<span class="note">只读 / 专员角色只能试算与确认，不能生成</span>'}
          </div>
          ${
            allocation
              ? `<div class="note">
                  算法 B：lift = 分群购买率 / 大盘购买率（${fmtPct(allocation.global_rate)}）；人均成本 = 触达 ${fmtMoney(
                    allocation.basis.reach_cost,
                  )} + 购买率 × 券面额；按 lift/成本 贪心排序；人均增量单量 ${allocation.basis.orders_per_converter}；
                  超出核心盘部分 ×${allocation.basis.tail_factor} 折减。${esc(allocation.log)}
                </div>`
              : ''
          }
        </div>
        ${
          allocation
            ? `<div class="panel-body" style="padding-top:0">${budgetBar(
                totals.spend,
                allocation.budget,
              )}</div>${allocationTable(allocation)}`
            : '<div class="panel-body"><div class="empty"><div class="mark">没有可投分群</div><p>提高预算或先重算分群。</p></div></div>'
        }
      </section>

      ${
        totals
          ? statStrip([
              { label: '名单人数', value: fmtNum(totals.audience), hint: `覆盖 ${totals.segments_allocated} 个分群` },
              { label: '券预算花费', value: fmtMoney(totals.spend), hint: `剩余 ${fmtMoney(totals.budget_remaining)}` },
              { label: '预计增量单量', value: fmtNum(totals.inc_orders), hint: '核心盘全效 + 溢出折减' },
              { label: '预计 ROI', value: Number(totals.roi).toFixed(2), hint: `增量 GMV ${fmtMoney(totals.inc_gmv, 0)}` },
            ])
          : ''
      }

      <section class="panel">
        <div class="panel-head">
          <h2>历史名单</h2>
          <span class="meta">${fmtNum(history.total)} 份 · 点击查看分群分配</span>
        </div>
        <div id="audience-history">${historyTable(history.items)}</div>
      </section>`;

    const campaignField = view.querySelector('[name=campaign_id]');
    const budgetField = view.querySelector('[name=budget]');
    campaignField?.addEventListener('change', () => {
      state.campaignId = campaignField.value;
      state.budget = '';
      audiencesPage.render(view, ctx);
    });
    view.querySelector('#preview')?.addEventListener('click', () => {
      state.budget = budgetField.value;
      audiencesPage.render(view, ctx);
    });
    view.querySelector('#generate')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      button.textContent = '占用中…';
      try {
        const audience = await api.post('/api/audiences', {
          campaign_id: Number(state.campaignId),
          budget: Number(budgetField.value || campaign.budget),
        });
        const resumed = audience.attempt > 1;
        toast(
          `${resumed ? '续跑' : '名单'} ${audience.audience_no}：实际占用 ${audience.member_count} 人，花费 ${fmtMoney(audience.spend)}，ROI ${Number(audience.roi).toFixed(2)}`,
        );
        state.budget = '';
        await audiencesPage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
        button.disabled = false;
        button.textContent = '生成名单';
      }
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      const audience = history.items.find((item) => String(item.id) === row.dataset.id);
      if (audience) {
        row.addEventListener('click', () => openAudience(view, ctx, audience, campaigns.items));
      }
    });
  },
};

export default audiencesPage;
