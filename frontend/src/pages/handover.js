import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {badge} from '../components/common/status.js';
import {topic} from '../constants/project.js';
import {date, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';
import {go} from '../router/hash.js';

const marker = (state, label)=>`<span class="status" data-state="${state}">${label}</span>`;

function readiness(item) {
  if (!item.run_id) return marker('queued', '尚未送检');
  if (item.status!=='complete') return badge(item.status);
  return item.passed ? marker('complete', '还原校验通过') : marker('failed', '未通过还原校验');
}

function aside(selected, operator) {
  if (!selected) return `<aside class="brief"><h2>批次交接</h2><p>修复组按批领用底片。批内每份底片的最新送检版都通过还原校验后，整批一起核发领用凭证。</p></aside>`;
  if (selected.voucher) {
    const voucher = selected.voucher;
    return `<aside class="brief"><h2>领用凭证 ${esc(voucher.code)}</h2>
      <p data-testid="voucher-state">已交接 · ${esc(voucher.issued_by)} · ${esc(date(voucher.issued_at))}</p>
      <p class="note">已交接的批次按原凭证查验，每份底片仍对应当时的报告。</p>
      <details><summary>凭证校验码</summary><code class="digest">${esc(voucher.digest)}</code></details></aside>`;
  }
  return `<aside class="brief"><h2>${esc(selected.title)}</h2>
    <p>待交接：批内每份底片的最新送检版都通过还原校验后，才能一起核发领用凭证。重新送检的底片以最新送检版为准，迟到的旧结果不能顶上。</p>
    ${operator?`<form id="handover-form" data-id="${selected.id}"><button>核验并核发领用凭证</button></form>`:'<p class="note">待经办员办理交接</p>'}</aside>`;
}

function panels(selected) {
  if (!selected) return '';
  if (selected.voucher) {
    const items = selected.voucher.items.map(item=>[esc(item.asset), esc(item.snapshot),
      `<a href="#runs/${item.run_id}">执行 ${item.run_id}</a>`,
      `<a href="#evidence/${item.evidence_id}">证据 ${item.evidence_id}</a>`,
      `<code class="digest">${esc(item.evidence_digest)}</code>`]);
    return panel('凭证明细（每份底片对应交接当时的报告）', records(['底片','送检版','执行','证据','证据校验码'], items));
  }
  const items = selected.items.map(item=>[esc(item.asset), esc(item.snapshot || '—'),
    item.run_id ? `<a href="#runs/${item.run_id}">执行 ${item.run_id}</a>` : '—', readiness(item)]);
  return panel('批内底片查验进度', records(['底片','最新送检版','执行','查验结论'], items));
}

export function handoverPage(state, identity) {
  const batches = state.batches || [];
  const selected = batches.find(row=>row.id===identity) || batches[0];
  const operator = state.session.role==='operator';
  const rows = batches.map(row=>[`<a href="#handover/${row.id}" ${row.id===selected?.id?'aria-current="true"':''}>${esc(row.title)}</a>`,
    row.voucher ? marker('complete','已交接') : marker('running','待交接'),
    esc(row.voucher?.code || '—'), esc(row.owner), esc(date(row.created_at))]);
  const creator = operator ? panel('建立交接批次', `<form id="batch-form">
    <label class="field"><span>批次名称</span><input name="title" required maxlength="120" placeholder="例如：L17 卷交付批"></label>
    <label class="field"><span>批内底片（每行一份底片文件名）</span><textarea name="assets" rows="4" required></textarea></label>
    <div class="actions form-actions"><button>保存批次</button></div></form>`) : '';
  return `${heading(topic.modules[4], '批内全部底片的最新送检版通过还原校验后，一起核发领用凭证。')}
    ${creator}
    <div class="spread"><section aria-label="批次目录">${records(['批次','状态','领用凭证','经办','建立时间'], rows, '尚未建立交接批次。')}</section>
    ${aside(selected, operator)}</div>
    ${panels(selected)}`;
}

export function bindHandover(state, api, update, fail) {
  submit('batch-form', async data=>{
    const assets = String(data.get('assets') || '').split('\n').map(line=>line.trim()).filter(Boolean);
    const batch = await api('/batches', {method:'POST', data:{title:data.get('title'), assets}});
    go(`handover/${batch.id}`);
  }, fail);
  const form = document.getElementById('handover-form');
  if (form) submit('handover-form', async()=>{
    await api(`/batches/${form.dataset.id}/handover`, {method:'POST'});
    await update();
  }, fail);
}
