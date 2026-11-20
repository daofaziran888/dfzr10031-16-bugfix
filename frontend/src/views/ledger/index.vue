<template>
  <section class="page" data-module="ledger">
    <header class="page-head">
      <div>
        <h2>关账账本</h2>
        <p class="page-desc">
          按业务日冻结每个仓、每个商品的结余：出入库与盘点先落账，关账后发布只读截面；
          晚到的补录不会改写首发批次，而是追加修订版，差额落在它真正发生的那一天。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="seedDemo">预置两日演示账本</button>
        <button class="btn" type="button" @click="reloadAll">刷新截面</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="loadDay">
      <label class="filter-item">
        <span>业务日</span>
        <input v-model="dayQuery" type="date" />
      </label>
      <label class="filter-item">
        <span>账本版本</span>
        <select v-model="editionQuery">
          <option value="">最新版</option>
          <option v-for="item in dayBatches" :key="item.batch_id" :value="String(item.edition)">
            第 {{ item.edition }} 版 · {{ item.batch_id }} · {{ item.kind === 'close' ? '关账' : '修订' }}
          </option>
        </select>
      </label>
      <label class="filter-item">
        <span>仓库</span>
        <input v-model="warehouseQuery" placeholder="如 WH-A，留空看全部" />
      </label>
      <button class="btn" type="submit">取数</button>
      <button class="btn ghost" type="button" @click="loadLatest">回到最新版</button>
    </form>

    <div v-if="day" class="ledger-panel">
      <div class="panel-head">
        <strong>{{ day.biz_date }}</strong>
        <span v-if="day.closed" class="tag ok">
          已关账 · 第 {{ day.edition }} 版 · {{ day.batch_id }} · {{ day.source === 'batch' ? '冻结截面' : '实时' }}
        </span>
        <span v-else class="tag warn">未关账 · 实时推导</span>
        <span v-if="day.supersedes" class="tag">取代 {{ day.supersedes }}</span>
        <span v-if="day.reason" class="tag">{{ day.reason }}</span>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th>仓库</th><th>商品</th><th>期初</th><th>当天出入库</th>
            <th>盘盈亏</th><th>账面</th><th>实盘</th><th>期末结余</th>
            <th>期初较上版</th><th>期末较上版</th><th>来源批次</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in day.rows" :key="`${row.warehouse}-${row.sku}`">
            <td>{{ row.warehouse }}</td>
            <td>{{ row.sku }}</td>
            <td>{{ row.opening_qty }}</td>
            <td :class="signClass(row.net_movement)">{{ signed(row.net_movement) }}</td>
            <td :class="signClass(row.count_adjustment)">{{ signed(row.count_adjustment) }}</td>
            <td>{{ row.book_qty }}</td>
            <td>{{ row.counted_qty ?? '—' }}</td>
            <td><strong>{{ row.closing_qty }}</strong></td>
            <td :class="signClass(row.opening_delta)">{{ deltaText(row.opening_delta) }}</td>
            <td :class="signClass(row.closing_delta)">{{ deltaText(row.closing_delta) }}</td>
            <td>
              <button
                v-for="batch in row.batch_ids || []"
                :key="batch"
                class="link"
                type="button"
                @click="openBatch(batch)"
              >{{ batch }}</button>
              <span v-if="!(row.batch_ids || []).length">—</span>
            </td>
          </tr>
          <tr v-if="!day.rows.length">
            <td colspan="11" class="empty-state">这天还没有出入库或盘点记录</td>
          </tr>
          <tr class="total-row">
            <td colspan="2">合计</td>
            <td>{{ day.totals.opening_qty }}</td>
            <td>{{ signed(day.totals.net_movement) }}</td>
            <td>{{ signed(day.totals.count_adjustment) }}</td>
            <td colspan="2"></td>
            <td>{{ day.totals.closing_qty }}</td>
            <td colspan="3">
              调拨守恒：{{ day.verified?.transfers_conserved ? '是' : '否' }} ·
              行内自洽：{{ day.verified?.rows_consistent ? '是' : '否' }}
            </td>
          </tr>
        </tbody>
      </table>

      <div class="sub-block">
        <h4>当天明细与采用批次</h4>
        <table class="data-table compact">
          <thead>
            <tr><th>仓库</th><th>类型</th><th>数量</th><th>批次 lot</th><th>单据</th><th>调拨号</th><th>登记时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="line in dayLines" :key="line.line_id">
              <td>{{ line.warehouse }}</td>
              <td>{{ line.kind }}</td>
              <td :class="signClass(line.qty)">{{ signed(line.qty) }}</td>
              <td>{{ line.lot || '—' }}</td>
              <td>{{ line.ref || '—' }}</td>
              <td>
                <button v-if="line.transfer_id" class="link" type="button" @click="traceTransfer(line.transfer_id)">
                  {{ line.transfer_id }}
                </button>
                <span v-else>—</span>
              </td>
              <td>{{ line.recorded_at }}</td>
            </tr>
            <tr v-if="!dayLines.length">
              <td colspan="7" class="empty-state">当天没有明细</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="grid-two">
      <div class="ledger-panel">
        <div class="panel-head"><strong>关账批次</strong><span class="muted">首发 + 每次修订</span></div>
        <table class="data-table compact">
          <thead><tr><th>批次</th><th>业务日</th><th>版本</th><th>类型</th><th>期末合计</th><th>原因</th></tr></thead>
          <tbody>
            <tr v-for="item in batches" :key="item.batch_id">
              <td><button class="link" type="button" @click="openBatch(item.batch_id)">{{ item.batch_id }}</button></td>
              <td>{{ item.biz_date }}</td>
              <td>{{ item.edition }}</td>
              <td>{{ item.kind === 'close' ? '关账' : '修订' }}</td>
              <td>{{ item.totals?.closing_qty }}</td>
              <td>{{ item.reason || item.note || '—' }}</td>
            </tr>
            <tr v-if="!batches.length"><td colspan="6" class="empty-state">还没有关账批次</td></tr>
          </tbody>
        </table>
      </div>

      <div class="ledger-panel">
        <div class="panel-head">
          <strong>两日核对</strong>
          <span class="muted">后一日期初 vs 前一日期末</span>
        </div>
        <form class="filter-bar tight" @submit.prevent="loadDelta">
          <label class="filter-item"><span>前一日</span><input v-model="deltaEarlier" type="date" /></label>
          <label class="filter-item"><span>后一日</span><input v-model="deltaLater" type="date" /></label>
          <button class="btn" type="submit">核对</button>
        </form>
        <table v-if="delta" class="data-table compact">
          <thead>
            <tr><th>仓库</th><th>前一日期末</th><th>后一日期初</th><th>衔接差额</th><th>后一日变动</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in delta.items" :key="`${item.warehouse}-${item.sku}`">
              <td>{{ item.warehouse }}</td>
              <td>{{ item.earlier_closing_qty ?? '—' }}</td>
              <td>{{ item.later_opening_qty ?? '—' }}</td>
              <td :class="signClass(item.carry_delta)">{{ deltaText(item.carry_delta) }}</td>
              <td :class="signClass(item.later_net_movement)">{{ signed(item.later_net_movement ?? 0) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="delta" class="muted">
          衔接{{ delta.balanced ? '一致' : '不一致' }}：
          {{ delta.earlier.biz_date }}（{{ delta.earlier.batch_id || '未关账' }}）→
          {{ delta.later.biz_date }}（{{ delta.later.batch_id || '未关账' }}）
        </p>
      </div>
    </div>

    <div class="grid-two">
      <div class="ledger-panel">
        <div class="panel-head"><strong>集市可用结余</strong><span class="muted">只认已关账批次</span></div>
        <form class="filter-bar tight" @submit.prevent="loadMarket">
          <label class="filter-item"><span>业务日</span><input v-model="marketDay" type="date" /></label>
          <button class="btn" type="submit">取数</button>
        </form>
        <p v-if="market && !market.available" class="muted">{{ market.reason }}</p>
        <template v-else-if="market">
          <table class="data-table compact">
            <thead><tr><th>仓库</th><th>可用结余</th><th>当天变动</th><th>来源批次</th><th>版本</th></tr></thead>
            <tbody>
              <tr v-for="item in market.warehouse_totals" :key="item.warehouse">
                <td>{{ item.warehouse }}</td>
                <td><strong>{{ item.available_qty }}</strong></td>
                <td>{{ signed(item.net_movement) }}</td>
                <td>{{ item.source_batch_id }}</td>
                <td>{{ item.edition }}</td>
              </tr>
            </tbody>
          </table>
          <p class="muted">截面批号 {{ market.source_batch_id }}，首发批号 {{ market.first_batch_id }}</p>
        </template>
      </div>

      <div class="ledger-panel">
        <div class="panel-head"><strong>补录登记</strong><span class="muted">出入库 / 调拨 / 盘点 / 关账</span></div>
        <form class="ledger-form" @submit.prevent="submitForm">
          <label class="modal-field">
            <span>登记类型</span>
            <select v-model="form.kind">
              <option value="movement">出入库变动</option>
              <option value="transfer">跨仓调拨</option>
              <option value="count">盘点合计</option>
              <option value="close">关账</option>
            </select>
          </label>
          <label class="modal-field"><span>业务日</span><input v-model="form.biz_date" type="date" required /></label>
          <template v-if="form.kind === 'close'">
            <label class="modal-field"><span>关账人</span><input v-model="form.operator" /></label>
            <label class="modal-field"><span>备注</span><input v-model="form.note" /></label>
          </template>
          <template v-else>
            <label class="modal-field"><span>仓库</span><input v-model="form.warehouse" placeholder="WH-A" /></label>
            <label v-if="form.kind === 'transfer'" class="modal-field">
              <span>调入仓</span><input v-model="form.to_warehouse" placeholder="WH-B" />
            </label>
            <label class="modal-field"><span>商品</span><input v-model="form.sku" placeholder="SKU-1001" /></label>
            <label v-if="form.kind === 'count'" class="modal-field">
              <span>盘点合计</span><input v-model.number="form.counted_qty" type="number" min="0" />
            </label>
            <label v-else class="modal-field">
              <span>数量（出库为负）</span><input v-model.number="form.qty" type="number" />
            </label>
            <label class="modal-field"><span>批次 lot</span><input v-model="form.lot" placeholder="LOT-0901" /></label>
            <label v-if="form.kind === 'transfer'" class="modal-field">
              <span>调拨单号</span><input v-model="form.transfer_id" placeholder="留空自动生成" />
            </label>
            <label v-if="form.kind === 'count'" class="modal-field">
              <span>盘点版本</span><input v-model="form.count_version" placeholder="v1 / v2" />
            </label>
            <label v-else class="modal-field"><span>单据号</span><input v-model="form.ref" /></label>
            <label class="modal-field">
              <span>幂等键（可选）</span><input v-model="form.idempotency_key" placeholder="重复提交同一笔时填同一个键" />
            </label>
          </template>
          <footer class="modal-foot">
            <button class="btn primary" type="submit" :disabled="submitting">提交</button>
          </footer>
        </form>
      </div>
    </div>

    <div v-if="message" class="notice" :class="message.ok ? 'ok' : 'bad'">{{ message.text }}</div>

    <div v-if="traceResult" class="ledger-panel">
      <div class="panel-head">
        <strong>补录溯源</strong>
        <span class="muted">{{ traceResult.transfer_id || traceResult.idempotency_key || traceResult.count_id }}</span>
        <button class="btn ghost" type="button" @click="traceResult = null">关闭</button>
      </div>
      <table class="data-table compact">
        <thead>
          <tr><th>仓库</th><th>业务日</th><th>期初</th><th>当天调整</th><th>期末结余</th><th>版本</th><th>命中批次</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in traceResult.days" :key="`${item.warehouse}-${item.biz_date}`">
            <td>{{ item.warehouse }}</td>
            <td>{{ item.biz_date }}</td>
            <td>{{ item.opening_qty }}</td>
            <td :class="signClass(item.day_adjustment)">{{ signed(item.day_adjustment) }}</td>
            <td><strong>{{ item.closing_qty }}</strong></td>
            <td>第 {{ item.edition }} 版</td>
            <td>
              <span v-for="batch in item.batches" :key="batch.batch_id" class="tag"
                    :class="batch.contains_line ? 'ok' : ''">
                {{ batch.batch_id }}（{{ batch.kind === 'close' ? '首发' : '修订' }}）
                {{ batch.contains_line ? '含此笔' : '不含' }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <Teleport to="body">
      <div v-if="batchDetail" class="modal-overlay" @click.self="batchDetail = null">
        <div class="modal-card wide">
          <header class="modal-head">
            <h3>{{ batchDetail.batch_id }} · {{ batchDetail.biz_date }} 第 {{ batchDetail.edition }} 版</h3>
            <button class="btn ghost" type="button" @click="batchDetail = null">&times;</button>
          </header>
          <div class="modal-body">
            <p class="muted">
              {{ batchDetail.kind === 'close' ? '关账批次' : '修订批次' }} · 发布 {{ batchDetail.published_at }}
              <template v-if="batchDetail.supersedes"> · 取代 {{ batchDetail.supersedes }}</template>
              <template v-if="batchDetail.reason"> · {{ batchDetail.reason }}</template>
            </p>
            <table class="data-table compact">
              <thead><tr><th>仓库</th><th>期初</th><th>变动</th><th>盘盈亏</th><th>期末</th></tr></thead>
              <tbody>
                <tr v-for="row in batchDetail.rows" :key="`${row.warehouse}-${row.sku}`">
                  <td>{{ row.warehouse }}</td>
                  <td>{{ row.opening_qty }}</td>
                  <td>{{ signed(row.net_movement) }}</td>
                  <td>{{ signed(row.count_adjustment) }}</td>
                  <td><strong>{{ row.closing_qty }}</strong></td>
                </tr>
              </tbody>
            </table>
            <p class="muted">冻结截面只读：刷新页面或重启服务后按此批号取数结果一致。</p>
          </div>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Line = {
  line_id: string
  kind: string
  qty: number
  lot: string
  ref: string
  transfer_id: string | null
  counterparty: string | null
  recorded_at: string
  warehouse?: string
}

type DayRow = {
  warehouse: string
  sku: string
  biz_date: string
  opening_qty: number
  net_movement: number
  count_adjustment: number
  book_qty: number
  counted_qty: number | null
  count_diff: number | null
  closing_qty: number
  opening_delta: number | null
  closing_delta: number | null
  batch_ids?: string[]
  batch_id?: string | null
  edition?: number | null
  lines: Line[]
}

type BatchSummary = {
  batch_id: string
  biz_date: string
  edition: number
  kind: string
  supersedes: string | null
  reason: string
  note: string
  totals: { closing_qty: number }
}

type DayView = {
  biz_date: string
  closed: boolean
  source: string
  batch_id: string | null
  edition: number | null
  supersedes: string | null
  reason: string
  verified: { transfers_conserved?: boolean; rows_consistent?: boolean }
  rows: DayRow[]
  totals: { opening_qty: number; net_movement: number; count_adjustment: number; closing_qty: number }
  batches: BatchSummary[]
}

type TraceDay = {
  warehouse: string
  biz_date: string
  opening_qty: number
  closing_qty: number
  edition: number
  day_adjustment: number
  batches: { batch_id: string; kind: string; contains_line: boolean }[]
}

const today = new Date().toISOString().slice(0, 10)
const dayQuery = ref('2026-09-01')
const editionQuery = ref('')
const warehouseQuery = ref('')
const deltaEarlier = ref('2026-09-01')
const deltaLater = ref('2026-09-02')
const marketDay = ref('2026-09-01')

const day = ref<DayView | null>(null)
const delta = ref<{ earlier: any; later: any; items: any[]; balanced: boolean } | null>(null)
const market = ref<any>(null)
const batches = ref<BatchSummary[]>([])
const batchDetail = ref<any>(null)
const traceResult = ref<{ transfer_id?: string; idempotency_key?: string; count_id?: string; days: TraceDay[] } | null>(null)
const message = ref<{ ok: boolean; text: string } | null>(null)
const submitting = ref(false)

const form = ref({
  kind: 'transfer',
  biz_date: '2026-09-01',
  warehouse: 'WH-A',
  to_warehouse: 'WH-B',
  sku: 'SKU-1001',
  qty: 10,
  counted_qty: 0,
  lot: 'LOT-0901',
  ref: '',
  transfer_id: '',
  count_version: 'v1',
  idempotency_key: '',
  operator: '值班管理员',
  note: '',
})

const dayBatches = computed(() => batches.value.filter((item) => item.biz_date === dayQuery.value))
const dayLines = computed<Line[]>(() => {
  const rows = day.value?.rows ?? []
  return rows.flatMap((row) => (row.lines ?? []).map((line) => ({ ...line, warehouse: row.warehouse })))
})

const stats = computed(() => {
  const closedDays = new Set(batches.value.map((item) => item.biz_date))
  const revisions = batches.value.filter((item) => item.kind === 'revision').length
  return [
    { label: '已关账业务日', value: closedDays.size },
    { label: '关账批次', value: batches.value.length },
    { label: '修订批次', value: revisions },
    { label: '当前截面结余', value: day.value?.totals.closing_qty ?? '—' },
  ]
})

function signed(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value > 0 ? `+${value}` : String(value)
}

function deltaText(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (value === 0) return '0'
  return value > 0 ? `+${value}` : String(value)
}

function signClass(value: number | null | undefined): string {
  if (!value) return ''
  return value > 0 ? 'pos' : 'neg'
}

async function loadDay() {
  const params = new URLSearchParams()
  if (editionQuery.value) params.set('edition', editionQuery.value)
  if (warehouseQuery.value) params.set('warehouse', warehouseQuery.value)
  try {
    const payload = await fetchJson<DayView>(`/api/ledger/days/${dayQuery.value}?${params.toString()}`)
    day.value = payload
    deltaEarlier.value = payload.biz_date
    message.value = null
  } catch (error) {
    message.value = { ok: false, text: error instanceof Error ? error.message : '账页读取失败' }
  }
}

async function loadLatest() {
  editionQuery.value = ''
  warehouseQuery.value = ''
  await loadDay()
}

async function loadBatches() {
  try {
    const payload = await fetchJson<{ items: BatchSummary[] }>('/api/ledger/batches')
    batches.value = payload.items ?? []
  } catch {
    batches.value = []
  }
}

async function loadDelta() {
  try {
    const params = new URLSearchParams({ earlier: deltaEarlier.value, later: deltaLater.value })
    delta.value = await fetchJson(`/api/ledger/delta?${params.toString()}`)
  } catch (error) {
    message.value = { ok: false, text: error instanceof Error ? error.message : '两日核对失败' }
  }
}

async function loadMarket() {
  const params = new URLSearchParams({ biz_date: marketDay.value })
  if (warehouseQuery.value) params.set('warehouse', warehouseQuery.value)
  try {
    market.value = await fetchJson(`/api/data_mart/balances?${params.toString()}`)
  } catch (error) {
    message.value = { ok: false, text: error instanceof Error ? error.message : '集市取数失败' }
  }
}

async function reloadAll() {
  await loadBatches()
  await loadDay()
  await loadDelta()
  await loadMarket()
}

async function openBatch(batchId: string) {
  try {
    const payload = await fetchJson<{ ok: boolean; batch: any }>(`/api/ledger/batches/${batchId}`)
    batchDetail.value = payload.batch
  } catch (error) {
    message.value = { ok: false, text: error instanceof Error ? error.message : '批次读取失败' }
  }
}

async function traceTransfer(transferId: string) {
  try {
    traceResult.value = await fetchJson(`/api/ledger/trace?transfer_id=${encodeURIComponent(transferId)}`)
  } catch (error) {
    message.value = { ok: false, text: error instanceof Error ? error.message : '溯源失败' }
  }
}

async function seedDemo() {
  try {
    const response = await request('/api/ledger/seed-demo', { method: 'POST' })
    const payload = await response.json()
    message.value = { ok: response.ok, text: payload.message ?? '已处理' }
    await reloadAll()
  } catch (error) {
    message.value = { ok: false, text: error instanceof Error ? error.message : '预置失败' }
  }
}

function buildPayload(): Record<string, unknown> {
  const base = form.value
  if (base.kind === 'close') {
    return { biz_date: base.biz_date, operator: base.operator, note: base.note }
  }
  if (base.kind === 'transfer') {
    return {
      from_warehouse: base.warehouse,
      to_warehouse: base.to_warehouse,
      sku: base.sku,
      biz_date: base.biz_date,
      qty: base.qty,
      lot: base.lot,
      ref: base.ref,
      transfer_id: base.transfer_id || undefined,
      idempotency_key: base.idempotency_key || undefined,
    }
  }
  if (base.kind === 'count') {
    return {
      warehouse: base.warehouse,
      sku: base.sku,
      biz_date: base.biz_date,
      counted_qty: base.counted_qty,
      lot: base.lot,
      count_version: base.count_version,
      idempotency_key: base.idempotency_key || undefined,
    }
  }
  return {
    warehouse: base.warehouse,
    sku: base.sku,
    biz_date: base.biz_date,
    qty: base.qty,
    lot: base.lot,
    ref: base.ref,
    idempotency_key: base.idempotency_key || undefined,
  }
}

const ENDPOINTS: Record<string, string> = {
  movement: '/api/ledger/movements',
  transfer: '/api/ledger/transfers',
  count: '/api/ledger/counts',
  close: '/api/ledger/close',
}

async function submitForm() {
  submitting.value = true
  message.value = null
  try {
    const response = await request(ENDPOINTS[form.value.kind], {
      method: 'POST',
      body: JSON.stringify({ values: buildPayload() }),
    })
    const payload = await response.json()
    message.value = { ok: Boolean(payload.ok), text: payload.message ?? '已提交' }
    if (payload.ok) {
      dayQuery.value = form.value.biz_date
      editionQuery.value = ''
      await reloadAll()
      const result = payload.entry
      if (form.value.kind === 'transfer' && result?.transfer_id) {
        await traceTransfer(result.transfer_id)
      }
    }
  } catch (error) {
    message.value = { ok: false, text: error instanceof Error ? error.message : '提交失败' }
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  await reloadAll()
  if (!batches.value.length) {
    dayQuery.value = today
    await loadDay()
  }
})
</script>

<style scoped>
.ledger-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 12px;
}
.panel-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 14px;
}
.tag {
  font-size: 12px;
  padding: 2px 6px;
  border-radius: 4px;
  background: #eef2f7;
  color: #334155;
}
.tag.ok { background: #e7f6ec; color: #17663a; }
.tag.warn { background: #fdf3e1; color: #8a5a12; }
.muted { color: var(--muted); font-size: 12px; }
.total-row td { font-weight: 600; background: #f8fafc; }
.compact th, .compact td { padding: 5px 8px; font-size: 12px; }
.grid-two { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.sub-block h4 { margin: 14px 0 6px; font-size: 13px; }
.filter-bar.tight { gap: 8px; margin-bottom: 8px; }
.ledger-form { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.ledger-form .modal-foot { grid-column: 1 / -1; }
.notice { padding: 10px 12px; border-radius: 6px; font-size: 13px; margin-bottom: 12px; }
.notice.ok { background: #e7f6ec; color: #17663a; }
.notice.bad { background: #fdecec; color: #b42318; }
.pos { color: #17663a; }
.neg { color: #b42318; }
.modal-card.wide { width: 640px; }
</style>
