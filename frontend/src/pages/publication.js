import {feedback} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {PUBLICATION} from '../constants/enums.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

function applyForm(spec, datasets, refresh) {
  const message = feedback();
  const submit = button('提交发布申请', {type:'submit', class:'primary', 'data-testid':'publish-submit'});
  return el('form', {class:'fields', 'data-testid':'publish-form', onSubmit:handler(submit, message, async event => {
    const data = new FormData(event.target);
    const markets = String(data.get('markets') || '').split(/[,，、;；\n]+/).map(text => text.trim()).filter(Boolean);
    if (!markets.length) throw new Error('请填写至少一个市场');
    await workspace.publish({dataset_id:Number(data.get('dataset_id')), day:data.get('day'), markets});
    event.target.reset();
    await refresh();
  })},
    el('label', {for:'publish-dataset', class:'field'}, el('span', {}, '门岗底稿'),
      el('select', {id:'publish-dataset', name:'dataset_id', required:true}, datasets.map((item, index) =>
        el('option', {value:item.id, selected:index === 0}, `${item.name} · ${item.row_count} 行`)))),
    el('label', {for:'publish-day', class:'field'}, el('span', {}, '观测日期'),
      el('input', {id:'publish-day', name:'day', type:'date', required:true})),
    el('label', {for:'publish-markets', class:'field'}, el('span', {}, '所选市场'),
      el('input', {id:'publish-markets', name:'markets', type:'text', required:true,
        placeholder:'如：南桥、西苑、滨河'}),
      el('small', {}, '用顿号或逗号分隔。合计为这些市场当天平均等候分钟之和。')),
    submit, message);
}

function ledgerTable(rows, error, refresh, canWithdraw) {
  return table([
    {key:'day', label:'观测日期'},
    {label:'所选市场', render:row=>row.markets.join('、')},
    {label:'合计（分钟）', render:row=>row.total},
    {label:'状态', render:row=>pill(PUBLICATION[row.state], row.state)},
    {key:'applicant', label:'申请人'},
    {label:'登记时间', render:row=>date(row.created_at)},
    {label:'说明', render:row=>row.detail || '—'},
    {label:'操作', render:row=>{
      if (row.state !== 'approved' || !canWithdraw) return '—';
      const action = button('撤下', {class:'quiet'});
      action.addEventListener('click', handler(action, error, async () => {
        if (!confirm(`确认撤下 ${row.day} 的这条合计？撤下后仍参与反推核查。`)) return;
        await workspace.withdraw(row.id, {note:'按公开口径撤回'});
        await refresh();
      }));
      return action;
    }},
  ], rows, {caption:'内部发布台账', empty:'还没有发布申请。'});
}

export async function publicationPage(route, refresh) {
  const {spec, datasets} = workspace;
  const [ledger, published] = await Promise.all([workspace.publications(), workspace.publicTotals()]);
  const error = feedback();
  const canWrite = session.canWrite && datasets.length;
  const publicTable = table([
    {key:'day', label:'观测日期'},
    {label:'所选市场', render:row=>row.markets.join('、')},
    {label:'市场数', render:row=>row.markets.length},
    {label:'平均等候合计（分钟）', render:row=>row.total},
    {label:'获准时间', render:row=>date(row.decided_at)},
  ], published, {caption:'已公开合计', empty:'还没有获准公开的合计。'});
  const download = button('下载公开 CSV', {class:'quiet', onClick:event => handler(download, error,
    () => workspace.download('/publications/public.csv', '等候合计_公开.csv'))(event)});
  return el('div', {},
    panel(spec.pages[6], p(spec.publication_note, 'subtle'),
      canWrite ? applyForm(spec, datasets, refresh)
        : p(session.canWrite ? '请先导入门岗底稿。' : '只读角色可以查看公开合计与内部台账。', 'subtle')),
    panel('公开区', publicTable, el('div', {class:'actions'}, download)),
    panel('内部发布台账', ledgerTable(ledger, error, refresh, session.canReview), error),
    canWrite ? null : empty('发布申请需要分析员或复核主管提交。', '#inputs', spec.pages[1]));
}
