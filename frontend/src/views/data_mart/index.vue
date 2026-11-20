<template>
  <section class="page" data-module="data_mart">
    <header class="page-head">
      <div>
        <h2>数据集市管理</h2>
        <p class="page-desc">维护集市表，围绕表编号、表名称、主题域、事实粒度做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记集市表</button>
        <button class="btn" type="button" @click="exportRows">导出数据集市清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无数据集市数据，可先登记集市表</td>
        </tr>
      </tbody>
    </table>

    <section class="market-block">
      <header class="panel-head">
        <strong>按仓可用结余</strong>
        <span class="muted">来自关账账本批次；未关账的日期不出数</span>
      </header>
      <form class="filter-bar" @submit.prevent="loadBalances">
        <label class="filter-item">
          <span>业务日</span>
          <input v-model="balanceDate" type="date" />
        </label>
        <label class="filter-item">
          <span>仓库</span>
          <input v-model="balanceWarehouse" placeholder="留空看全部" />
        </label>
        <button class="btn" type="submit">取数</button>
      </form>
      <p v-if="balances && !balances.available" class="muted">{{ balances.reason }}</p>
      <table v-else-if="balances" class="data-table compact">
        <thead>
          <tr><th>仓库</th><th>可用结余</th><th>当天变动</th><th>盘盈亏</th><th>来源批次</th><th>版本</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in balances.warehouse_totals" :key="item.warehouse">
            <td>{{ item.warehouse }}</td>
            <td>{{ item.available_qty }}</td>
            <td>{{ item.net_movement }}</td>
            <td>{{ item.count_adjustment }}</td>
            <td>{{ item.source_batch_id }}</td>
            <td>{{ item.edition }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="balances?.available" class="muted">
        截面 {{ balances.source_batch_id }}（第 {{ balances.edition }} 版），首发 {{ balances.first_batch_id }}；
        第 {{ balances.edition }} 版以外的历史截面可在「关账账本」按批号回看。
      </p>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据集市记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <Teleport to="body">
      <div v-if="showCreateModal" class="modal-overlay" @click.self="showCreateModal = false">
        <div class="modal-card">
          <header class="modal-head">
            <h3>登记集市表</h3>
            <button class="btn ghost" type="button" @click="showCreateModal = false">&times;</button>
          </header>
          <form class="modal-body" @submit.prevent="submitCreate">
            <label v-for="field in columns.slice(0, 3)" :key="field" class="modal-field">
              <span>{{ field }}</span>
              <input v-model="createFields[field]" :placeholder="`请输入${field}`" required />
            </label>
            <footer class="modal-foot">
              <button class="btn" type="button" @click="showCreateModal = false">取消</button>
              <button class="btn primary" type="submit">提交登记</button>
            </footer>
          </form>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/data_mart'
const columns = ["表编号", "表名称", "主题域", "事实粒度", "关联维度", "度量字段", "刷新周期", "表状态"]
const actions = ["提交建模", "发布集市", "下线集市"]
const statuses = ["草稿", "已建模", "已发布", "已下线"]
const stats = [{"label": "已发布集市", "value": 0}, {"label": "草稿集市", "value": 0}, {"label": "已下线集市", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreateModal = ref(false)
const createFields = ref<Record<string, string>>({})
const balanceDate = ref('2026-09-01')
const balanceWarehouse = ref('')
const balances = ref<any>(null)

function updateStats() {
  const data = rows.value
  stats.length > 0 && (stats[0].value = data.filter((r: Row) => r.status === statuses[1] || r.pending).length)
  stats.length > 1 && (stats[1].value = data.filter((r: Row) => r.status && !r.pending && !r.abnormal).length)
  stats.length > 2 && (stats[2].value = data.filter((r: Row) => r.abnormal).length)
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = ''
  createFields.value = {}
  showCreateModal.value = true
}

async function submitCreate() {
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: createFields.value }),
    })
    if (!response.ok) {
      const data = await response.json()
      throw new Error(data.message || '登记失败')
    }
    showCreateModal.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('数据集市动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据集市操作失败'
  }
}

async function loadBalances() {
  const params = new URLSearchParams({ biz_date: balanceDate.value })
  if (balanceWarehouse.value) params.set('warehouse', balanceWarehouse.value)
  try {
    const response = await request(`/api/data_mart/balances?${params.toString()}`)
    if (!response.ok) {
      throw new Error('可用结余读取失败')
    }
    balances.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '可用结余读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('集市表列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    updateStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据集市列表读取失败'
  }
}

onMounted(async () => {
  await reload()
  await loadBalances()
})
</script>

<style scoped>
.market-block { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin: 12px 0; }
.panel-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; font-size: 14px; }
.muted { color: var(--muted); font-size: 12px; }
.compact th, .compact td { padding: 5px 8px; font-size: 12px; }
</style>
